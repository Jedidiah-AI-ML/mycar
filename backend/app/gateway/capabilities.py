"""Capability profile per device type (PRD 7).

What an adapter *can* do, by default. A device may later report that something isn't
available on this car (e.g. the ECU refuses Mode 06); those reports live in
`devices.capabilities` and switch the default off. Features that need a missing
capability are switched off for the vehicle and the UI says so, never a false "all good".
"""

CAPABILITIES = (
    "live_pids", "mode03", "mode07", "mode0a", "mode06", "freeze_frame", "battery_voltage",
    "cranking_voltage", "gnss", "accelerometer", "vin", "remote_commands",
)  # fmt: skip

DEFAULTS: dict[str, dict[str, bool]] = {
    # Freematics ONE+ with the AutoCare firmware (PRD 7): all of them.
    "freematics_one_plus": dict.fromkeys(CAPABILITIES, True),
}


def effective(device_type: str, reported: dict | None, location_consent: bool) -> dict[str, bool]:
    """Default for the type, minus anything the device reported missing.

    GNSS also needs the owner's location consent (PRD 10): without it, positions are
    dropped at the door, so for the user's purposes the car has no GNSS.
    """
    caps = dict(DEFAULTS.get(device_type, dict.fromkeys(CAPABILITIES, False)))
    for name, value in (reported or {}).items():
        if name in caps and value is False:
            caps[name] = False
    caps["gnss"] = caps["gnss"] and location_consent
    return caps
