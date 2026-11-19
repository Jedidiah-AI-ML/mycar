"""Adapter for the Freematics ONE+ running the AutoCare firmware (payload v1, ADR-006).

Clock-skew correction (plan step 8, SEED P8): the device stamps `sent_at` with its own
clock when it uploads. We compare that with a trusted clock taken at the same moment:
the GNSS time in the batch if the device has a fix, otherwise the server's receive time.
If they differ by more than `clock_skew_threshold_ms` (2 s), every time in the batch is
shifted by the difference. Small differences are left alone: they're mostly network
latency, not a wrong clock. A device whose clock restarted at 1970 after a reboot
(ADR-010) is shifted by decades, which is exactly right.
"""

import json

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.gateway import auth
from app.gateway.adapters.base import NormalisedBatch, NormalisedCritical, PayloadError
from app.gateway.contract import (
    AckV1,
    BatchV1,
    Command,
    CommandResult,
    CrankEvent,
    CriticalV1,
    DtcEvent,
    FreezeFrame,
    Mode06Event,
    PidsSupported,
    TripEnd,
    TripStart,
    UnknownEvent,
    VinEvent,
)
from app.gateway.signals import catalogue
from app.models import Device, DeviceCommand


def clock_offset_ms(sent_at: int, reference_ms: int) -> int:
    offset = reference_ms - sent_at
    return offset if abs(offset) > get_settings().clock_skew_threshold_ms else 0


def _decode(body: bytes, model):
    try:
        return model.model_validate(json.loads(body))
    except json.JSONDecodeError as e:
        raise PayloadError(f"body is not JSON: {e.msg}") from None
    except ValidationError as e:
        first = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5]
        )
        raise PayloadError(f"payload doesn't match contract v1: {first}") from None


class FreematicsOnePlus:
    device_type = "freematics_one_plus"

    async def authenticate(self, session: AsyncSession, authorization: str | None) -> Device | None:
        device = await auth.authenticate_device(session, authorization)
        return device if device is not None and device.type == self.device_type else None

    # ------------------------------------------------------------ batches

    def parse_batch(self, body: bytes, device_id: str, received_ms: int) -> NormalisedBatch:
        b: BatchV1 = _decode(body, BatchV1)
        if b.device_id != device_id:
            raise PayloadError("device_id in the body doesn't match the device token")
        offset = clock_offset_ms(b.sent_at, b.gnss_time if b.gnss_time is not None else received_ms)
        base = b.t0 + offset
        cat = catalogue().by_name
        out = NormalisedBatch(
            device_id=b.device_id, boot=b.boot, trip=b.trip, seq=b.seq, final=b.final,
            received_ms=received_ms, clock_offset_ms=offset, firmware=b.fw,
            rssi=b.net.rssi, iccid=b.net.iccid,
        )  # fmt: skip

        for name, rows in b.s.items():
            sig = cat.get(name)
            if sig is None:  # newer firmware than this server: ignore, never reject
                out.dropped_unknown_signals += len(rows)
                continue
            for dt, value in rows:
                if sig.in_range(value):
                    out.readings.append((base + dt, sig.id, value))
                else:
                    out.dropped_out_of_range += 1
        out.positions = [(base + dt, lat, lon, spd) for dt, lat, lon, spd in b.g]

        crank_id = cat["crank_v"].id
        for ev in b.ev:
            ts = base + ev.t
            match ev:
                case TripStart():
                    out.trip_start_ms = ts
                case TripEnd():
                    out.trip_end_ms = ts
                    out.trip_events.append({"type": "trip_end", "ts": ts, "reason": ev.reason})
                case VinEvent():
                    out.vin = ev.vin
                case PidsSupported():
                    out.protocol, out.pids_mode01 = ev.protocol, ev.mode01
                case DtcEvent():
                    out.dtc_lists.append((ts, ev.mode, list(ev.codes)))
                case FreezeFrame():
                    out.freeze_frames.append((ts, ev.code, dict(ev.values)))
                case Mode06Event():
                    out.mode06 += [
                        (ts, r.mid, r.tid, r.uasid, r.value, r.min, r.max) for r in ev.results
                    ]
                case CrankEvent():
                    step = 1000 / ev.rate_hz
                    out.readings += [
                        (ts + int(i * step), crank_id, v)
                        for i, v in enumerate(ev.volts)
                        if cat["crank_v"].in_range(v)
                    ]
                case UnknownEvent():
                    out.trip_events.append({**ev.model_dump(exclude={"t"}), "ts": ts})
                case CommandResult():
                    out.cmd_results.append((ev.command_id, ev.status, ev.detail))
                case _:  # readiness, harsh: kept with the trip
                    out.trip_events.append({**ev.model_dump(exclude={"t"}), "ts": ts})
        return out

    # ------------------------------------------------------------ critical packets

    def parse_critical(self, body: bytes, device_id: str, received_ms: int) -> NormalisedCritical:
        c: CriticalV1 = _decode(body, CriticalV1)
        if c.device_id != device_id:
            raise PayloadError("device_id in the body doesn't match the device token")
        offset = clock_offset_ms(c.sent_at, received_ms)
        ts = c.t + offset
        cat = catalogue().by_name
        readings = [
            (ts, cat[n].id, v) for n, v in c.values.items() if n in cat and cat[n].in_range(v)
        ]
        return NormalisedCritical(
            device_id=c.device_id, boot=c.boot, trip=c.trip, kind=c.kind, ts_ms=ts,
            received_ms=received_ms, clock_offset_ms=offset, readings=readings,
            position=tuple(c.pos) if c.pos else None,
        )  # fmt: skip

    # ------------------------------------------------------------ replies

    def ack(self, *, dup: bool, server_time_ms: int, commands: list[DeviceCommand]) -> dict:
        cmds = [Command(**self.encode_command(c)) for c in commands]
        return AckV1(ok=True, dup=dup, server_time=server_time_ms, cmds=cmds).model_dump()

    def encode_command(self, command: DeviceCommand) -> dict:
        return {"id": command.id, "type": command.type.value, "payload": command.payload or {}}
