"""Device uploads (ADR-002, ADR-006).

    POST /ingest/{device_type}/batches    gzip JSON batch   -> ack
    POST /ingest/{device_type}/critical   small, immediate  -> ack

Checks, in order: known device type (404), device token (401), rate limit (429), body
size before and after decompression (413, against gzip bombs), contract (422). Then the
adapter normalises the payload and `gateway.store` writes it in one transaction.
"""

import logging
import time
import zlib
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import SessionDep
from app.core.config import get_settings
from app.gateway.adapters.base import PayloadError
from app.gateway.ratelimit import RateLimiter
from app.gateway.registry import get_adapter
from app.gateway.store import store_batch, store_critical

log = logging.getLogger("app.ingest")
router = APIRouter(prefix="/ingest", tags=["ingest"])
limiter = RateLimiter(get_settings().ingest_rate_per_min)


def server_now_ms() -> int:
    """The server's clock. A dependency so tests (and the local gate run) can set it."""
    return int(time.time() * 1000)


NowMs = Annotated[int, Depends(server_now_ms)]


async def _read_body(request: Request, encoding: str | None) -> bytes:
    s = get_settings()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > s.ingest_max_body_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > s.ingest_max_body_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Body too large")
    if (encoding or "identity").lower() == "identity":
        if len(body) > s.ingest_max_decoded_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Body too large")
        return bytes(body)
    if encoding.lower() != "gzip":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Use gzip or no encoding")
    d = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(bytes(body), s.ingest_max_decoded_bytes + 1)
    except zlib.error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not valid gzip") from None
    if len(out) > s.ingest_max_decoded_bytes or d.unconsumed_tail:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Decompressed body too large")
    if not d.eof:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is truncated gzip")
    return out


async def _authorise(device_type: str, session, authorization: str | None):
    adapter = get_adapter(device_type)
    if adapter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device type")
    device = await adapter.authenticate(session, authorization)
    if device is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid device token",
            headers={"WWW-Authenticate": "Bearer"},
        )  # fmt: skip
    wait = limiter.take(device.id)
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests",
            headers={"Retry-After": str(max(int(wait + 0.999), 1))},
        )  # fmt: skip
    return adapter, device


@router.post("/{device_type}/batches")
async def ingest_batch(
    device_type: str,
    request: Request,
    session: SessionDep,
    now_ms: NowMs,
    authorization: Annotated[str | None, Header()] = None,
    content_encoding: Annotated[str | None, Header()] = None,
):
    adapter, device = await _authorise(device_type, session, authorization)
    raw = await _read_body(request, content_encoding)
    try:
        nb = adapter.parse_batch(raw, device.id, now_ms)
    except PayloadError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    wire_bytes = int(request.headers.get("content-length") or 0) or len(raw)
    res = await store_batch(session, device, nb, wire_bytes)
    await session.commit()
    log.info(
        "batch",
        extra={"device_id": device.id, "key": list(nb.key), "dup": res.dup,
               "discarded": res.discarded, "readings": res.n_readings,
               "offset_ms": nb.clock_offset_ms},
    )  # fmt: skip
    return JSONResponse(adapter.ack(dup=res.dup, server_time_ms=now_ms, commands=res.commands))


@router.post("/{device_type}/critical")
async def ingest_critical(
    device_type: str,
    request: Request,
    session: SessionDep,
    now_ms: NowMs,
    authorization: Annotated[str | None, Header()] = None,
    content_encoding: Annotated[str | None, Header()] = None,
):
    adapter, device = await _authorise(device_type, session, authorization)
    raw = await _read_body(request, content_encoding)
    try:
        nc = adapter.parse_critical(raw, device.id, now_ms)
    except PayloadError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    res = await store_critical(session, device, nc, len(raw))
    await session.commit()
    log.warning(
        "critical packet",
        extra={"device_id": device.id, "kind": nc.kind, "dup": res.dup,
               "discarded": res.discarded},
    )  # fmt: skip
    return JSONResponse(adapter.ack(dup=res.dup, server_time_ms=now_ms, commands=res.commands))
