# SPDX-License-Identifier: MPL-2.0
"""Immutable, validated transaction data."""
from dataclasses import dataclass
from ._validation import octets, uint
from .encoding import base16_decode

TRANSFER = "01" + "00" * 31
STAKE = "02" + "00" * 31


def contract_bytes(value):
    return octets(base16_decode(value) if isinstance(value, str) else value, "contract id", 32)


def nonempty(value, name):
    value = octets(value, name)
    if not value:
        raise ValueError(f"{name} cannot be empty")
    return value


@dataclass(frozen=True)
class Memo:
    data: bytes

    def __post_init__(self):
        value = self.data.encode("utf-8") if isinstance(self.data, str) else self.data
        object.__setattr__(self, "data", octets(value, "memo"))


@dataclass(frozen=True)
class ContractCall:
    contract_id: bytes
    fn_name: str
    fn_args: bytes = b""
    committed: bool = False

    def __post_init__(self):
        object.__setattr__(self, "contract_id", contract_bytes(self.contract_id))
        if not isinstance(self.fn_name, str) or not self.fn_name or "\0" in self.fn_name:
            raise ValueError("a nonempty function name is required")
        self.fn_name.encode("utf-8")
        object.__setattr__(self, "fn_args", octets(self.fn_args, "function arguments"))
        if type(self.committed) is not bool:
            raise TypeError("committed must be bool")


@dataclass(frozen=True)
class DeployData:
    bytecode: bytes
    owner: bytes
    init_args: bytes | None = b""
    nonce: int = 0

    def __post_init__(self):
        object.__setattr__(self, "bytecode", nonempty(self.bytecode, "bytecode"))
        object.__setattr__(self, "owner", nonempty(self.owner, "owner"))
        if self.init_args is not None:
            object.__setattr__(self, "init_args", octets(self.init_args, "initialization arguments"))
        uint(self.nonce, 64, "deployment nonce")
