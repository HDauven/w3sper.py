# SPDX-License-Identifier: MPL-2.0
"""Owned state snapshots returned by syncers and bookkeepers."""
from dataclasses import dataclass
import struct
from ._validation import octets, uint


@dataclass(frozen=True)
class AccountBalance:
    nonce: int
    value: int

    def __post_init__(self):
        uint(self.nonce, 64, "account nonce")
        uint(self.value, 64, "account balance")


@dataclass(frozen=True)
class AddressBalance:
    value: int
    spendable: int

    def __post_init__(self):
        uint(self.value, 64, "address balance")
        uint(self.spendable, 64, "spendable balance")
        if self.spendable > self.value:
            raise ValueError("spendable exceeds balance")


@dataclass(frozen=True)
class StakeAmount:
    value: int
    locked: int = 0
    eligibility: int = 0

    def __post_init__(self):
        for name in ("value", "locked", "eligibility"):
            uint(getattr(self, name), 64, name)
        uint(self.total, 64, "total stake")

    @property
    def total(self):
        return self.value + self.locked


@dataclass(frozen=True)
class StakeInfo:
    amount: StakeAmount | None = None
    reward: int = 0
    faults: int = 0
    hard_faults: int = 0

    def __post_init__(self):
        if self.amount is not None and not isinstance(self.amount, StakeAmount):
            raise TypeError("amount must be StakeAmount or None")
        uint(self.reward, 64, "reward")
        uint(self.faults, 8, "faults")
        uint(self.hard_faults, 8, "hard faults")

    @classmethod
    def parse(cls, data):
        data = octets(data, "stake archive", 56)
        if data[0] not in (0, 1) or (data[0] and data[8] not in (0, 1)):
            raise ValueError("invalid stake option tag")
        if not data[0]:
            return cls()
        amount = StakeAmount(*struct.unpack_from("<QQQ", data, 16)) if data[8] else None
        return cls(amount, int.from_bytes(data[40:48], "little"), data[48], data[49])
