"""The device adapter interface (ADR-002) and the canonical batch every adapter produces.

Everything after the gateway (store, trips, indicators, alerts) sees only
`NormalisedBatch` / `NormalisedCritical`: canonical signal ids, corrected UTC times in
epoch ms, typed records. It never imports adapter code.
"""

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceCommand


class PayloadError(ValueError):
    """The body isn't a valid payload for this device type (-> 422)."""


@dataclass
class NormalisedBatch:
    device_id: str
    boot: int
    trip: int
    seq: int
    final: bool
    received_ms: int
    clock_offset_ms: int  # added to every device time (0 if within the threshold)
    firmware: str | None = None
    rssi: int | None = None
    iccid: str | None = None
    readings: list[tuple[int, int, float]] = field(default_factory=list)  # ts, signal_id, value
    positions: list[tuple[int, float, float, float | None]] = field(default_factory=list)
    dtc_lists: list[tuple[int, str, list[str]]] = field(default_factory=list)  # ts, mode, codes
    freeze_frames: list[tuple[int, str, dict]] = field(default_factory=list)  # ts, code, values
    mode06: list[tuple[int, int, int, int, int, int, int]] = field(default_factory=list)
    vin: str | None = None
    protocol: str | None = None
    pids_mode01: str | None = None
    trip_start_ms: int | None = None
    trip_end_ms: int | None = None
    trip_events: list[dict] = field(default_factory=list)  # readiness, harsh, unknown
    cmd_results: list[tuple[int, str, str | None]] = field(default_factory=list)
    dropped_unknown_signals: int = 0
    dropped_out_of_range: int = 0

    @property
    def key(self) -> tuple[str, int, int, int]:
        return (self.device_id, self.boot, self.trip, self.seq)


@dataclass
class NormalisedCritical:
    device_id: str
    boot: int
    trip: int
    kind: str
    ts_ms: int  # corrected
    received_ms: int
    clock_offset_ms: int
    readings: list[tuple[int, int, float]]
    position: tuple[float, float] | None


class DeviceAdapter(Protocol):
    device_type: str

    async def authenticate(self, session: AsyncSession, authorization: str | None) -> Device | None:
        """The device for these credentials, or None (-> 401)."""

    def parse_batch(self, body: bytes, device_id: str, received_ms: int) -> NormalisedBatch:
        """Decoded body -> canonical rows. Raises PayloadError."""

    def parse_critical(self, body: bytes, device_id: str, received_ms: int) -> NormalisedCritical:
        """Decoded body -> canonical critical packet. Raises PayloadError."""

    def ack(self, *, dup: bool, server_time_ms: int, commands: list[DeviceCommand]) -> dict:
        """The response body the device expects."""

    def encode_command(self, command: DeviceCommand) -> dict:
        """One server -> device command in the device's format."""
