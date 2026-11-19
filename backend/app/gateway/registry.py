"""`devices.type` -> adapter (ADR-002). v1 registers exactly one."""

from app.gateway.adapters.base import DeviceAdapter
from app.gateway.adapters.freematics_one_plus import FreematicsOnePlus

_ADAPTERS: dict[str, DeviceAdapter] = {a.device_type: a for a in (FreematicsOnePlus(),)}


def get_adapter(device_type: str) -> DeviceAdapter | None:
    return _ADAPTERS.get(device_type)
