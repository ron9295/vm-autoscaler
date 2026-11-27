"""Parse, format and round Kubernetes byte quantities (storage sizes).

Sizes are handled in bytes as an int ("512Mi" -> 536870912, "30Gi" -> 32212254720).
Decimal is used for the growth step so "1.1 * 4" never becomes "4.4000000001".
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, Decimal, InvalidOperation

# Ordered from smallest to largest unit.
BINARY_SUFFIXES: dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_DECIMAL_SUFFIXES: dict[str, int] = {
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}
_QUANTITY_RE = re.compile(r"^\s*(?P<number>\d+(\.\d+)?|\.\d+)\s*(?P<suffix>[A-Za-z]*)\s*$")

GIB = 1024**3


class QuantityError(ValueError):
    """The string is not a valid Kubernetes quantity of the expected kind."""


def _split(text: str) -> tuple[Decimal, str]:
    match = _QUANTITY_RE.match(str(text))
    if match is None:
        raise QuantityError(f"not a valid quantity: {text!r}")
    try:
        return Decimal(match.group("number")), match.group("suffix")
    except InvalidOperation as exc:  # pragma: no cover - the regex already guards this
        raise QuantityError(f"not a valid number: {text!r}") from exc


def parse_bytes(text: str) -> int:
    """Parse a size quantity into bytes. Accepts "512Mi", "30Gi", "1Ti", "1G"."""
    number, suffix = _split(text)
    if suffix == "":
        multiplier = 1
    elif suffix in BINARY_SUFFIXES:
        multiplier = BINARY_SUFFIXES[suffix]
    elif suffix in _DECIMAL_SUFFIXES:
        multiplier = _DECIMAL_SUFFIXES[suffix]
    else:
        raise QuantityError(f"unsupported byte suffix in {text!r}")
    return int((number * multiplier).to_integral_value(rounding=ROUND_CEILING))


def format_bytes(value: int) -> str:
    """Format bytes with the largest binary suffix that divides exactly ("2Ti", "20Gi")."""
    for suffix in ("Ei", "Pi", "Ti", "Gi", "Mi", "Ki"):
        multiplier = BINARY_SUFFIXES[suffix]
        if value >= multiplier and value % multiplier == 0:
            return f"{value // multiplier}{suffix}"
    return str(value)


def round_up_to_gib(value: int) -> int:
    """Round bytes up to the next whole GiB."""
    return -(-value // GIB) * GIB


def grow_bytes(current: int, step_percent: float) -> int:
    """Increase bytes by step_percent and round up to a whole GiB."""
    factor = 1 + Decimal(str(step_percent)) / 100
    grown = int((Decimal(current) * factor).to_integral_value(rounding=ROUND_CEILING))
    return round_up_to_gib(grown)
