"""Write one normalised batch (or critical packet) to the database, in ONE transaction.

The order matters:
1. Record when the device was last seen and how much data it used (always, even for
   duplicates and unclaimed devices, so "online" and the data meter stay true).
2. No car linked -> nothing is stored (Q3: data from before a claim belongs to nobody).
3. Insert the batch key. If it already exists this is a retry: stop, store nothing,
   answer `dup: true`. Because the key and the rows commit together, a crash halfway
   leaves neither, and the device's retry stores everything once.
4. Trip (found or created by the device's own counters), readings (COPY: the fastest
   way into Postgres), positions **only if the owner consented to location**, DTCs,
   freeze frames, Mode 06, VIN / protocol / supported PIDs, command results.
5. Queue `trip.assemble`, debounced: a burst of batches gives one assembly run.
6. Hand out any commands waiting for this device.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, bindparam, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.gateway.adapters.base import NormalisedBatch, NormalisedCritical
from app.jobs.queue import enqueue
from app.models import (
    ConsentPurpose,
    Device,
    DeviceAssignment,
    DeviceBatch,
    DeviceCommand,
    DeviceCommandStatus,
    DtcEvent,
    DtcStatus,
    Job,
    Mode06Result,
    Trip,
    Vehicle,
    VinSource,
    positions,
)
from app.services.consents import owner_has_consent
from app.services.vin import normalise_vin

DTC_STATUS = {"03": DtcStatus.stored, "07": DtcStatus.pending, "0A": DtcStatus.permanent}
READING_COLUMNS = ["vehicle_id", "trip_id", "ts", "signal_id", "value"]


def to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass
class StoreResult:
    dup: bool = False
    discarded: bool = False  # device not linked to a car
    trip_id: uuid.UUID | None = None
    n_readings: int = 0
    n_positions: int = 0
    commands: list[DeviceCommand] = field(default_factory=list)


# ---------------------------------------------------------------- shared steps


async def _touch_device(session, device: Device, received_ms: int, body_bytes: int, nb=None):
    month = to_dt(received_ms).date().replace(day=1)
    values = {
        "last_seen_at": to_dt(received_ms),
        # Add to this month's counter, or start a new month.
        "bytes_month": case(
            (Device.bytes_month_start == month, Device.bytes_month + body_bytes),
            else_=body_bytes,
        ),
        "bytes_month_start": month,
    }
    if nb is not None:
        if nb.rssi is not None:
            values["last_rssi"] = nb.rssi
        if nb.firmware:
            values["firmware_version"] = nb.firmware
        if nb.iccid:
            values["sim_iccid"] = nb.iccid
    await session.execute(update(Device).where(Device.id == device.id).values(**values))


async def _vehicle_for(session, device_id: str) -> uuid.UUID | None:
    return await session.scalar(
        select(DeviceAssignment.vehicle_id).where(
            DeviceAssignment.device_id == device_id, DeviceAssignment.unassigned_at.is_(None)
        )
    )


async def _trip_id(session, vehicle_id, device_id: str, boot: int, trip: int) -> uuid.UUID:
    stmt = (
        insert(Trip)
        .values(vehicle_id=vehicle_id, device_id=device_id, device_boot=boot, device_trip=trip)
        .on_conflict_do_update(
            index_elements=[Trip.device_id, Trip.device_boot, Trip.device_trip],
            set_={"updated_at": func.now()},
        )
        .returning(Trip.id)
    )
    return await session.scalar(stmt)


async def _copy_readings(session, vehicle_id, trip_id, rows) -> int:
    if not rows:
        return 0
    records = [(vehicle_id, trip_id, to_dt(ts), sid, float(v)) for ts, sid, v in rows]
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    # COPY runs on the same connection, inside the same transaction as everything else.
    await raw.driver_connection.copy_records_to_table(
        "readings", records=records, columns=READING_COLUMNS
    )
    return len(records)


async def _pending_commands(session, device_id: str) -> list[DeviceCommand]:
    resend_before = datetime.now(UTC) - timedelta(seconds=get_settings().command_resend_s)
    cmds = (
        await session.scalars(
            select(DeviceCommand)
            .where(
                DeviceCommand.device_id == device_id,
                or_(
                    DeviceCommand.status == DeviceCommandStatus.queued,
                    and_(  # sent, but the device never confirmed it: its ack may be lost
                        DeviceCommand.status == DeviceCommandStatus.sent,
                        DeviceCommand.sent_at < resend_before,
                    ),
                ),
            )
            .order_by(DeviceCommand.id)
            .limit(10)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for c in cmds:
        c.status, c.sent_at = DeviceCommandStatus.sent, datetime.now(UTC)
    return list(cmds)


# ---------------------------------------------------------------- batches


async def store_batch(
    session: AsyncSession, device: Device, nb: NormalisedBatch, body_bytes: int
) -> StoreResult:
    res = StoreResult()
    await _touch_device(session, device, nb.received_ms, body_bytes, nb)
    res.commands = await _pending_commands(session, device.id)

    vehicle_id = await _vehicle_for(session, device.id)
    if vehicle_id is None:
        res.discarded = True
        await _apply_cmd_results(session, device.id, nb)
        return res

    key = (
        insert(DeviceBatch)
        .values(
            device_id=nb.device_id,
            boot=nb.boot,
            trip=nb.trip,
            seq=nb.seq,
            received_at=to_dt(nb.received_ms),
            clock_offset_ms=nb.clock_offset_ms,
            n_samples=len(nb.readings),
            bytes=body_bytes,
        )  # fmt: skip
        .on_conflict_do_nothing()
        .returning(DeviceBatch.seq)
    )
    if (await session.execute(key)).first() is None:
        res.dup = True
        return res

    trip_id = await _trip_id(session, vehicle_id, device.id, nb.boot, nb.trip)
    res.trip_id = trip_id
    await session.execute(
        update(DeviceBatch)
        .where(
            DeviceBatch.device_id == nb.device_id,
            DeviceBatch.boot == nb.boot,
            DeviceBatch.trip == nb.trip,
            DeviceBatch.seq == nb.seq,
        )  # fmt: skip
        .values(trip_id=trip_id)
    )
    await _update_trip(session, trip_id, nb)
    res.n_readings = await _copy_readings(session, vehicle_id, trip_id, nb.readings)

    if nb.positions and await owner_has_consent(session, vehicle_id, ConsentPurpose.location):
        await session.execute(
            positions.insert(),
            [
                {"vehicle_id": vehicle_id, "trip_id": trip_id, "ts": to_dt(ts), "lat": lat,
                 "lon": lon, "speed_kmh": spd}
                for ts, lat, lon, spd in nb.positions
            ],
        )  # fmt: skip
        res.n_positions = len(nb.positions)

    await _store_dtcs(session, vehicle_id, trip_id, nb)
    if nb.mode06:
        session.add_all(
            Mode06Result(
                vehicle_id=vehicle_id, trip_id=trip_id, ts=to_dt(ts), mid=mid, tid=tid,
                uasid=uasid, value=value, min=lo, max=hi,
            )
            for ts, mid, tid, uasid, value, lo, hi in nb.mode06
        )  # fmt: skip
    await _store_vehicle_info(session, vehicle_id, nb)
    await _apply_cmd_results(session, device.id, nb)

    await enqueue(
        session,
        "trip.assemble",
        {"trip_id": str(trip_id)},
        run_at=datetime.now(UTC) + timedelta(seconds=get_settings().trip_assemble_delay_s),
        dedupe_key=f"trip.assemble:{trip_id}",
    )
    return res


async def _update_trip(session, trip_id, nb: NormalisedBatch) -> None:
    values: dict = {}
    if nb.trip_start_ms is not None:
        start = to_dt(nb.trip_start_ms)
        values["started_at"] = func.least(func.coalesce(Trip.started_at, start), start)
    if nb.trip_end_ms is not None:
        end = to_dt(nb.trip_end_ms)
        values["ended_at"] = func.greatest(func.coalesce(Trip.ended_at, end), end)
    if nb.trip_events:
        # Atomic append: concurrent batches of one trip can't lose each other's events.
        values["events"] = Trip.events.op("||")(bindparam("new_events", nb.trip_events, JSONB))
    if values:
        await session.execute(update(Trip).where(Trip.id == trip_id).values(**values))


async def _store_dtcs(session, vehicle_id, trip_id, nb: NormalisedBatch) -> None:
    """Each Mode 03/07/0A list is the car's full list at that moment: codes in it are
    (still) active, codes missing from it were cleared. A late batch can't clear a code
    that a newer report has already seen (last_seen guard)."""
    for ts_ms, mode, codes in sorted(nb.dtc_lists):
        status, ts = DTC_STATUS[mode], to_dt(ts_ms)
        # A newer report already cleared these: an older one arriving late can't reopen them.
        cleared_later = (
            set(
                (
                    await session.scalars(
                        select(DtcEvent.code).where(
                            DtcEvent.vehicle_id == vehicle_id,
                            DtcEvent.status == status,
                            DtcEvent.code.in_(codes),
                            DtcEvent.cleared_at >= ts,
                        )
                    )
                ).all()
            )
            if codes
            else set()
        )
        for code in codes:
            if code in cleared_later:
                continue
            stmt = insert(DtcEvent).values(
                vehicle_id=vehicle_id, code=code, status=status, first_seen=ts, last_seen=ts,
                trip_id=trip_id,
            )  # fmt: skip
            stmt = stmt.on_conflict_do_update(
                index_elements=[DtcEvent.vehicle_id, DtcEvent.code, DtcEvent.status],
                index_where=DtcEvent.cleared_at.is_(None),
                set_={
                    "first_seen": func.least(DtcEvent.first_seen, stmt.excluded.first_seen),
                    "last_seen": func.greatest(DtcEvent.last_seen, stmt.excluded.last_seen),
                },
            )
            await session.execute(stmt)
        clear = update(DtcEvent).where(
            DtcEvent.vehicle_id == vehicle_id,
            DtcEvent.status == status,
            DtcEvent.cleared_at.is_(None),
            DtcEvent.last_seen < ts,
        )
        if codes:
            clear = clear.where(DtcEvent.code.not_in(codes))
        await session.execute(clear.values(cleared_at=ts))
    for _ts, code, values in nb.freeze_frames:
        await session.execute(
            update(DtcEvent)
            .where(
                DtcEvent.vehicle_id == vehicle_id,
                DtcEvent.code == code,
                DtcEvent.cleared_at.is_(None),
                DtcEvent.freeze_frame.is_(None),
            )
            .values(freeze_frame=values)
        )


async def _store_vehicle_info(session, vehicle_id, nb: NormalisedBatch) -> None:
    values: dict = {}
    if nb.protocol:
        values["protocol"] = nb.protocol
    if nb.pids_mode01:
        values["supported_pids"] = {"mode01": nb.pids_mode01}
    if values:
        await session.execute(update(Vehicle).where(Vehicle.id == vehicle_id).values(**values))
    if not nb.vin:
        return
    try:
        vin = normalise_vin(nb.vin)
    except ValueError:
        return  # some ECUs report a short or padded VIN; the owner can type it in
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle.vin is not None:
        return  # never overwrite: a mismatch is for a person to look at (Phase 4 insight)
    taken = await session.scalar(
        select(Vehicle.id).where(
            Vehicle.vin == vin, Vehicle.deleted_at.is_(None), Vehicle.id != vehicle_id
        )
    )
    if taken is None:
        vehicle.vin, vehicle.vin_source = vin, VinSource.device


async def _apply_cmd_results(session, device_id: str, nb: NormalisedBatch) -> None:
    for cmd_id, status, detail in nb.cmd_results:
        await session.execute(
            update(DeviceCommand)
            .where(
                DeviceCommand.id == cmd_id,
                DeviceCommand.device_id == device_id,
                DeviceCommand.status.in_([DeviceCommandStatus.queued, DeviceCommandStatus.sent]),
            )
            .values(
                status=DeviceCommandStatus.done if status == "done" else DeviceCommandStatus.failed,
                done_at=func.now(),
                result={"status": status, "detail": detail},
            )
        )


# ---------------------------------------------------------------- critical packets


async def store_critical(
    session: AsyncSession, device: Device, nc: NormalisedCritical, body_bytes: int
) -> StoreResult:
    """Stored immediately and handed to a priority-0 job (alert rules: Phase 6).

    A critical packet has no sequence number; its retry is recognised by its alert job:
    one per device, boot, trip and kind (the device sends each kind once per trip).
    """
    res = StoreResult()
    await _touch_device(session, device, nc.received_ms, body_bytes)
    res.commands = await _pending_commands(session, device.id)
    vehicle_id = await _vehicle_for(session, device.id)
    if vehicle_id is None:
        res.discarded = True
        return res
    dedupe = f"critical:{nc.device_id}:{nc.boot}:{nc.trip}:{nc.kind}"
    if await session.scalar(select(Job.id).where(Job.dedupe_key == dedupe)) is not None:
        res.dup = True
        return res
    trip_id = await _trip_id(session, vehicle_id, device.id, nc.boot, nc.trip)
    res.trip_id = trip_id
    res.n_readings = await _copy_readings(session, vehicle_id, trip_id, nc.readings)
    if nc.position and await owner_has_consent(session, vehicle_id, ConsentPurpose.location):
        lat, lon = nc.position
        await session.execute(
            positions.insert(),
            [{"vehicle_id": vehicle_id, "trip_id": trip_id, "ts": to_dt(nc.ts_ms), "lat": lat,
              "lon": lon, "speed_kmh": None}],
        )  # fmt: skip
        res.n_positions = 1
    await enqueue(
        session,
        "alert.critical",
        {
            "vehicle_id": str(vehicle_id), "trip_id": str(trip_id), "device_id": nc.device_id,
            "kind": nc.kind, "ts_ms": nc.ts_ms,
            "values": {str(sid): v for _, sid, v in nc.readings},
        },
        priority=0,
        dedupe_key=dedupe,
    )  # fmt: skip
    return res
