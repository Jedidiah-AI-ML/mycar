"""What a device can do (PRD 7 "capability profile").

The Freematics ONE+ can do everything here; other adapters (or a car that refuses Mode 06)
can't. Switching one off removes exactly that data, so the backend can be tested on
"this car has no Mode 06" without special cases.
"""

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class Capabilities:
    mode03: bool = True  # stored DTCs
    mode07: bool = True  # pending DTCs
    mode0a: bool = True  # permanent DTCs
    mode06: bool = True  # on-board test results
    freeze_frame: bool = True
    readiness: bool = True
    vin: bool = True
    batt_v: bool = True  # device measures battery voltage itself
    crank: bool = True  # high-rate cranking-voltage capture
    gnss: bool = True
    accel: bool = True
    commands: bool = True  # remote commands (health scan, poll rates)

    def without(self, *names: str) -> "Capabilities":
        known = {f.name for f in fields(self)}
        unknown = set(names) - known
        if unknown:
            raise ValueError(f"unknown capabilities {sorted(unknown)}")
        return replace(self, **dict.fromkeys(names, False))


FULL = Capabilities()
