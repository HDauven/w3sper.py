# SPDX-License-Identifier: MPL-2.0
"""Moonlight-only orchestration; no submission, nonce reservation or sync."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ._validation import uint
from .encoding import base58_decode
from .profile import Profile, SigningProfile
from .values import Gas, validate_chain_id
from .walletcore import WalletCore


@dataclass(frozen=True)
class AccountBalance:
    nonce: int
    value: int

    def __post_init__(self):
        uint(self.nonce, 64, "account nonce")
        uint(self.value, 64, "account balance")


@dataclass(frozen=True)
class TransferResult:
    buffer: bytes
    hash: str
    chain_id: int
    nonce: int


def _account(identifier: str) -> bytes:
    key = base58_decode(identifier)
    if len(key) == 64:
        raise NotImplementedError("Phoenix transfers and balances are not supported")
    if len(key) != 96:
        raise ValueError("a 96-byte Moonlight account identifier is required")
    return key


def _balance(lookup, account: bytes) -> AccountBalance:
    if lookup is None:
        raise ValueError("an account_balance callback is required")
    balance = lookup(account)
    if not isinstance(balance, AccountBalance):
        raise TypeError("account_balance must return AccountBalance")
    return balance


@dataclass(frozen=True)
class Bookkeeper:
    """Bind profiles and an optional caller-supplied account snapshot lookup.

    The callback receives the raw 96-byte account and returns AccountBalance.
    It is NOT a network syncer; callers own freshness and nonce coordination.
    """

    core: WalletCore
    account_balance: Callable[[bytes], AccountBalance] | None = field(default=None, repr=False)

    def balance(self, identifier: str) -> AccountBalance:
        return _balance(self.account_balance, _account(identifier))

    def bind(self, profile: Profile | SigningProfile) -> "BookEntry":
        if not isinstance(profile, (Profile, SigningProfile)):
            raise TypeError("a Profile or SigningProfile is required")
        return BookEntry(profile, self)


@dataclass(frozen=True)
class BookEntry:
    profile: Profile | SigningProfile
    bookkeeper: Bookkeeper

    @property
    def public_profile(self) -> Profile:
        return self.profile.profile if isinstance(self.profile, SigningProfile) else self.profile

    def balance(self) -> AccountBalance:
        return _balance(self.bookkeeper.account_balance, self.public_profile.account)

    def transfer(self) -> "Transfer":
        return Transfer(self)


@dataclass(frozen=True)
class Transfer:
    """Immutable builder. Every fluent method returns a new independent snapshot.

    nonce(n) is the CURRENT/PREVIOUS account nonce, just like the pinned Go
    builder; build() signs with n + 1. WalletCore.moonlight takes the final nonce.
    """

    entry: BookEntry
    _amount: int = 0
    _deposit: int = 0
    _gas: Gas = field(default_factory=Gas)
    _chain: int | None = None
    _nonce: int | None = None
    _receiver: bytes | None = field(default=None, repr=False)

    def amount(self, value: int) -> "Transfer":
        return replace(self, _amount=uint(value, 64, "amount"))

    def deposit(self, value: int) -> "Transfer":
        return replace(self, _deposit=uint(value, 64, "deposit"))

    def gas(self, config: Gas) -> "Transfer":
        if not isinstance(config, Gas):
            raise TypeError("gas must be a Gas configuration")
        uint(config.limit, 64, "gas limit")
        uint(config.price, 64, "gas price")
        return replace(self, _gas=config)

    def chain(self, chain_id: int) -> "Transfer":
        return replace(self, _chain=validate_chain_id(chain_id))

    def nonce(self, current_nonce: int) -> "Transfer":
        return replace(self, _nonce=uint(current_nonce, 64, "current nonce"))

    def to(self, identifier: str) -> "Transfer":
        return replace(self, _receiver=_account(identifier))

    def build(self) -> TransferResult:
        signer = self.entry.profile
        if not isinstance(signer, SigningProfile):
            raise ValueError("public profiles cannot sign; derive a SigningProfile from a seed")
        if self._receiver is None:
            raise ValueError("a receiver is required")
        if self._chain is None:
            raise ValueError("chain id is required")
        current_nonce = self._nonce
        if current_nonce is None:
            current_nonce = self.entry.balance().nonce
        uint(current_nonce, 64, "current nonce")
        final_nonce = uint(current_nonce + 1, 64, "next nonce")
        if self.entry.bookkeeper.core is None:
            raise ValueError("a WalletCore instance is required")
        buffer, tx_hash = self.entry.bookkeeper.core.moonlight(
            signer._seed, signer.index, self._receiver, self._amount,
            deposit=self._deposit, gas_limit=self._gas.limit, gas_price=self._gas.price,
            nonce=final_nonce, chain_id=self._chain,
        )
        return TransferResult(buffer, tx_hash, self._chain, final_nonce)
