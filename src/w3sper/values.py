# SPDX-License-Identifier: MPL-2.0
"""Exact unit conversions and small public value types."""

import re
from dataclasses import dataclass

from ._validation import integer, octets, uint

DECIMALS = 9
LUX_PER_DUSK = 10**DECIMALS
DEFAULT_GAS_LIMIT = 2_900_000_000
DEFAULT_GAS_PRICE = 1


def format_to_dusk(lux: int) -> str:
    """Format a non-negative Lux integer, without rounding or trailing zeros."""
    integer(lux, "Lux")
    if lux < 0:
        raise ValueError("Lux must be non-negative")
    whole, fraction = divmod(lux, LUX_PER_DUSK)
    return str(whole) if not fraction else f"{whole}.{fraction:09d}".rstrip("0")


def parse_from_dusk(dusk: str) -> int:
    """Parse ASCII decimal text with at most nine fractional digits; no floats."""
    if not isinstance(dusk, str):
        raise TypeError("Dusk must be decimal text")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{0,9})?", dusk):
        raise ValueError("Dusk must be non-negative decimal text with at most 9 fractional digits")
    whole, _, fraction = dusk.partition(".")
    return int(whole) * LUX_PER_DUSK + int(fraction.ljust(DECIMALS, "0"))


def validate_chain_id(chain_id: int) -> int:
    return uint(chain_id, 8, "chain id")


@dataclass(frozen=True)
class Gas:
    """None/non-positive values select Go SDK defaults; signing enforces u64."""

    limit: int | None = None
    price: int | None = None

    def __post_init__(self):
        for name, default in (("limit", DEFAULT_GAS_LIMIT), ("price", DEFAULT_GAS_PRICE)):
            value = getattr(self, name)
            if value is not None:
                integer(value, f"gas {name}")
            object.__setattr__(self, name, default if value is None or value <= 0 else value)

    @property
    def total(self) -> int:
        return self.limit * self.price


@dataclass(frozen=True)
class Bookmark:
    """An owned eight-byte little-endian cursor. All-ff means end-of-sync."""

    data: bytes

    def __post_init__(self):
        object.__setattr__(self, "data", octets(self.data, "bookmark", 8))

    @classmethod
    def from_int(cls, value: int) -> "Bookmark":
        return cls(uint(value, 64, "bookmark").to_bytes(8, "little"))

    def __int__(self) -> int:
        return int.from_bytes(self.data, "little")

    def __bytes__(self) -> bytes:
        return self.data

    def __str__(self) -> str:
        return self.data.hex()

    @property
    def is_none(self) -> bool:
        return self.data == b"\xff" * 8
