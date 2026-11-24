"""The one way AutoCare calls an LLM (ADR-013).

`complete_json()` sends a system + user prompt through **LiteLLM** (provider-agnostic; the
model is `LLM_MODEL`, currently gpt-4o-mini through OpenRouter) and asks for a JSON object back.
- **No key** (or a blank model) -> nothing is sent; callers fall back to templates.
- **Budget cap:** if this month's recorded spend has reached `LLM_MONTHLY_BUDGET_USD`, no
  call is made.
- **Every attempt is logged** in `llm_calls`: purpose, model, prompt version, input hash,
  tokens in/out, cost (LiteLLM's price table), ok/error, latency (PRD 5).
- One call per invocation; the caller decides about retries (explain.py retries once).
  A **fatal** error (bad key, unknown model, no credit) is flagged so nobody retries it.
- Error text is scrubbed of anything that looks like an API key before it is stored.

Tests set `acompletion` to a fake: no network, no cost.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache

from sqlalchemy import func, select

from app.core.config import get_settings
from app.models import LlmCall

log = logging.getLogger("app.llm")
acompletion = None  # tests put a fake here; otherwise LiteLLM's, loaded on first use


@cache
def _litellm():
    """Import LiteLLM only when a call is actually made: it's a heavy import (tens of
    seconds cold on Windows), and API start-up and requests that never reach the LLM
    shouldn't pay for it."""
    # The price list bundled with the pinned version: no download at import time (no
    # surprise network call, and costs change only when we upgrade).
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    litellm.telemetry = False
    return litellm


@dataclass
class LlmResult:
    ok: bool
    data: dict | None = None
    error: str | None = None
    skipped: str | None = None  # why no call was made: no_key / over_budget
    fatal: bool = False  # retrying can't help (bad key, unknown model, no credit)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


def enabled() -> bool:
    s = get_settings()
    return bool(s.llm_api_key.get_secret_value() and s.llm_model)


async def spent_this_month(session) -> float:
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(LlmCall.ts >= start)
    )
    return float(total)


FATAL_ERRORS = ("AuthenticationError", "PermissionDeniedError", "NotFoundError")
KEY_RE = re.compile(r"(sk|key)-[A-Za-z0-9*_\-]{6,}")


def _price(model: str) -> tuple[float, float] | None:
    """Per-token (input, output) price for `model`, or for the base model a router such as
    OpenRouter passes through (openrouter/openai/gpt-4o-mini -> gpt-4o-mini)."""
    table = _litellm().model_cost
    for name in (model, model.split("/", 1)[-1], model.rsplit("/", 1)[-1]):
        c = table.get(name) or {}
        if c.get("input_cost_per_token") is not None:
            return float(c["input_cost_per_token"]), float(c.get("output_cost_per_token") or 0)
    return None


def _cost(response) -> float:
    try:
        return float(_litellm().completion_cost(completion_response=response))
    except Exception as e:  # noqa: BLE001 - not in the price table: work it out from the tokens
        log.debug("completion_cost failed (%s); using the token price", type(e).__name__)
    model = get_settings().llm_model
    price = _price(model)
    usage = getattr(response, "usage", None)
    if price is None or usage is None:
        log.warning("no price for %s: this call is recorded at $0", model)
        return 0.0
    return (usage.prompt_tokens or 0) * price[0] + (usage.completion_tokens or 0) * price[1]


async def complete_json(
    session,
    *,
    purpose: str,
    prompt_version: str,
    system: str,
    user: str,
    input_hash: str,
    vehicle_id=None,
) -> LlmResult:
    s = get_settings()
    if not enabled():
        return LlmResult(ok=False, skipped="no_key")
    if await spent_this_month(session) >= s.llm_monthly_budget_usd:
        log.warning("LLM budget reached; not calling", extra={"purpose": purpose})
        return LlmResult(ok=False, skipped="over_budget")

    started = time.perf_counter()
    result = LlmResult(ok=False)
    try:
        call = acompletion or _litellm().acompletion
        response = await call(
            model=s.llm_model,
            api_key=s.llm_api_key.get_secret_value(),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.2,
            timeout=s.llm_timeout_s,
        )
        usage = getattr(response, "usage", None)
        result.tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        result.tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        result.cost_usd = _cost(response)
        text = response.choices[0].message.content or ""
        result.data = json.loads(text)
        if not isinstance(result.data, dict):
            raise ValueError("the answer is not a JSON object")
        result.ok = True
    except json.JSONDecodeError as e:
        result.error = f"not JSON: {e.msg}"
    except Exception as e:  # noqa: BLE001 - recorded on the call; the caller falls back
        result.error = KEY_RE.sub("[key]", f"{type(e).__name__}: {e}")[:500]
        result.fatal = type(e).__name__ in FATAL_ERRORS
    session.add(
        LlmCall(
            purpose=purpose, model=s.llm_model, prompt_version=prompt_version,
            input_hash=input_hash, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            cost_usd=Decimal(str(round(result.cost_usd, 6))), ok=result.ok,
            error=result.error, latency_ms=round((time.perf_counter() - started) * 1000),
            vehicle_id=vehicle_id,
        )
    )  # fmt: skip
    await session.flush()
    return result
