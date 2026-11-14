"""VIN checks.

A VIN is 17 characters from A-Z and 0-9, never I, O or Q (too easy to confuse with
1 and 0). Position 9 is a check digit, but it's only mandatory for vehicles built for
North America. Many Nigerian imports are US-market cars, but not all, so a wrong
check digit is a *warning*, not an error.  # SEED - validate (S4)
"""

import re

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

_TRANSLIT = {
    **{str(d): d for d in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9), strict=True)),
    **dict(zip("JKLMN", range(1, 6), strict=True)),
    "P": 7,
    "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10), strict=True)),
}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def normalise_vin(raw: str) -> str:
    vin = raw.strip().upper().replace(" ", "").replace("-", "")
    if len(vin) != 17:
        raise ValueError("VIN must be exactly 17 characters")
    if not _VIN_RE.match(vin):
        raise ValueError("VIN may only contain A-Z and 0-9, and never the letters I, O or Q")
    return vin


def check_digit_ok(vin: str) -> bool:
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS, strict=True))
    expected = "X" if total % 11 == 10 else str(total % 11)
    return vin[8] == expected


def vin_warnings(vin: str | None) -> list[str]:
    if vin and not check_digit_ok(vin):
        return [
            "VIN check digit doesn't match. That's normal for many non-US-market cars; "
            "otherwise please double-check the VIN."
        ]
    return []
