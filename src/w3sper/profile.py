# SPDX-License-Identifier: MPL-2.0
"""Public profiles and explicitly seed-bearing signing profiles."""

from dataclasses import dataclass, field
from threading import Lock

from ._validation import octets, uint
from .encoding import base58_encode
from .walletcore import WalletCore

ADDRESS_KEY_SIZE = 64
ACCOUNT_KEY_SIZE = 96
PROFILE_SIZE = 160
MAX_PROFILE_INDEX = 255


@dataclass(frozen=True)
class Profile:
    """Public bytes only. Parsing a Profile never grants signing authority.

    Length validation is not curve-point validation; wallet-core checks account
    keys when signing a transfer to them.
    """

    data: bytes = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "data", octets(self.data, "profile", PROFILE_SIZE))

    def __bytes__(self) -> bytes:
        return self.data

    @property
    def address(self) -> bytes:
        return self.data[:ADDRESS_KEY_SIZE]

    @property
    def account(self) -> bytes:
        return self.data[ADDRESS_KEY_SIZE:]

    @property
    def address_id(self) -> str:
        return base58_encode(self.address)

    @property
    def account_id(self) -> str:
        return base58_encode(self.account)


@dataclass(frozen=True, init=False)
class SigningProfile:
    """Seed-bearing authority, with public keys derived by wallet-core.

    Seeds are excluded from repr, not securely erasable in Python. Do not log,
    pickle or dataclasses.asdict() this object. Export only bytes(signer.profile).
    """

    profile: Profile
    index: int
    _seed: bytes = field(repr=False, compare=False)

    def __init__(self, core: WalletCore, seed: bytes, index: int = 0):
        seed = octets(seed, "seed", 64)
        uint(index, 8, "profile index")
        public = Profile(core.generate_profile(seed, index))
        object.__setattr__(self, "profile", public)
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "_seed", seed)


class Generator:
    """Deterministic, cached signing profiles for one copied 64-byte seed.

    default() reserves index 0; next() starts at 1 regardless of call order.
    at() only looks up an already generated profile. derive() accepts 0..255.
    """

    def __init__(self, seed: bytes, core: WalletCore):
        self._seed = octets(seed, "seed", 64)
        self._core = core
        self._profiles: dict[int, SigningProfile] = {}
        self._next_index = 1
        self._lock = Lock()

    def _derive(self, index: int) -> SigningProfile:
        uint(index, 8, "profile index")
        if index not in self._profiles:
            self._profiles[index] = SigningProfile(self._core, self._seed, index)
        return self._profiles[index]

    def derive(self, index: int) -> SigningProfile:
        with self._lock:
            return self._derive(index)

    def default(self) -> SigningProfile:
        return self.derive(0)

    def next(self) -> SigningProfile:
        with self._lock:
            profile = self._derive(self._next_index)
            self._next_index += 1
            return profile

    def at(self, index: int) -> SigningProfile | None:
        uint(index, 8, "profile index")
        with self._lock:
            return self._profiles.get(index)

    def index_of(self, profile: SigningProfile | Profile) -> int:
        with self._lock:
            if isinstance(profile, SigningProfile) and self._profiles.get(profile.index) is profile:
                return profile.index
            return -1

    def __len__(self) -> int:
        with self._lock:
            return len(self._profiles)
