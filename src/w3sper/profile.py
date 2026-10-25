# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""Public identities and separately owned, disposable child-key capabilities."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock

from ._validation import octets, uint
from .encoding import base58_encode
from .cancellation import CancellationToken
from .walletcore import WalletCore

ADDRESS_KEY_SIZE = 64
ACCOUNT_KEY_SIZE = 96
PROFILE_SIZE = 160
MAX_PROFILE_INDEX = 255


class DisposedError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    """Public bytes only. Parsing an identity never grants signing authority."""
    data: bytes = field(repr=False)
    index: int = field(default=-1, compare=False)
    _source: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "data", octets(self.data, "profile", PROFILE_SIZE))
        if self.index != -1:
            uint(self.index, 8, "profile index")

    def __bytes__(self):
        return self.data

    @property
    def address(self):
        return self.data[:64]

    @property
    def account(self):
        return self.data[64:]

    @property
    def address_id(self):
        return base58_encode(self.address)

    @property
    def account_id(self):
        return base58_encode(self.account)

    def same_source_of(self, other):
        other = other.profile if isinstance(other, ProfileSigner) else other
        return isinstance(other, Profile) and self._source is not None and self._source is other._source


class ProfileSigner:
    """Owns derived BLS/Phoenix/view keys, never a master seed.

    dispose() rejects new uses and stale in-flight results, and best-effort wipes
    owned keys/snapshots. Python and Wasmtime cannot guarantee memory erasure.
    Public identity remains readable. Already sent transactions cannot be revoked.
    """
    __slots__ = ("_profile", "_material", "_core", "_lock", "_disposed", "_snapshots", "_cancel")

    def __init__(self, core: WalletCore, seed, index=0):
        material = core.generate_profile_keys(seed, index)
        try:
            self._initialize(core, Profile(material[160:], index, object()), material[:160])
        finally:
            material[:] = bytes(len(material))

    def _initialize(self, core, profile, material):
        self._profile = profile
        self._material = bytearray(octets(material, "profile keys", 160))
        self._core = core
        self._lock = RLock()
        self._disposed = False
        self._snapshots = []
        self._cancel = CancellationToken()

    @classmethod
    def _issue(cls, core, profile, material):
        self = cls.__new__(cls)
        self._initialize(core, profile, material)
        return self

    @property
    def profile(self):
        return self._profile

    @property
    def index(self):
        return self._profile.index

    @property
    def account(self):
        return self._profile.account

    @property
    def address(self):
        return self._profile.address

    @property
    def account_id(self):
        return self._profile.account_id

    @property
    def address_id(self):
        return self._profile.address_id

    def same_source_of(self, other):
        return self.profile.same_source_of(other)

    def assert_alive(self):
        with self._lock:
            if self._disposed:
                raise DisposedError("ProfileSigner has been disposed")
        return self

    @property
    def disposed(self):
        with self._lock:
            return self._disposed

    @contextmanager
    def _keys(self, kind, *, check_result=True):
        ranges = {"bls": (0, 32), "phoenix": (32, 96), "view": (96, 160), "both": (0, 96)}
        with self._lock:
            self.assert_alive()
            start, end = ranges[kind]
            snapshot = self._material[start:end]
            self._snapshots.append(snapshot)
        try:
            yield snapshot
            if check_result:
                self.assert_alive()
        finally:
            with self._lock:
                snapshot[:] = bytes(len(snapshot))
                self._snapshots = [item for item in self._snapshots if item is not snapshot]

    def sign_bls_message(self, message):
        message = octets(message, "message")
        with self._keys("bls") as key:
            public, signature = self._core.sign_bls_message_with_key(key, message)
            if public != self.account:
                raise ValueError("Profile key does not match its account")
            return signature

    def dispose(self):
        with self._lock:
            self._disposed = True
            self._material[:] = bytes(len(self._material))
            for snapshot in self._snapshots:
                snapshot[:] = bytes(len(snapshot))
        self._cancel.cancel(DisposedError("ProfileSigner has been disposed"))

    close = dispose

    def __enter__(self):
        return self.assert_alive()

    def __exit__(self, *exc):
        self.dispose()

    def __reduce__(self):
        raise TypeError("signing capabilities cannot be pickled")

    def __repr__(self):
        return f"ProfileSigner(index={self.index}, disposed={self.disposed})"


# Compatibility spelling: direct construction now retains child keys, not a seed.
SigningProfile = ProfileSigner


class ProfileGenerator:
    """Cache public identities; consult a seed/provider only for derivation/issuance.

    signer(profile) issues a fresh capability and checks the current provider
    against the captured identity. close() wipes an owned seed but does not revoke
    independently issued signers; the application owns those lifetimes.
    """
    def __init__(self, seed, core: WalletCore):
        self._seed = None if callable(seed) else bytearray(octets(seed, "seed", 64))
        self._provider = seed if callable(seed) else lambda: self._seed
        self._core = core
        self._profiles = {}
        self._source = object()
        self._next_index = 1
        self._lock = RLock()
        self._closed = False

    def _alive(self):
        if self._closed:
            raise DisposedError("ProfileGenerator has been closed")

    def _seed_copy(self):
        self._alive()
        return bytearray(octets(self._provider(), "seed", 64))

    def derive(self, index):
        uint(index, 8, "profile index")
        with self._lock:
            self._alive()
            if index not in self._profiles:
                seed = self._seed_copy()
                try:
                    data = self._core.generate_profile(seed, index)
                    self._profiles[index] = Profile(data, index, self._source)
                finally:
                    seed[:] = bytes(len(seed))
            return self._profiles[index]

    def default(self):
        return self.derive(0)

    def next(self):
        with self._lock:
            profile = self.derive(self._next_index)
            self._next_index += 1
            return profile

    def at(self, index):
        uint(index, 8, "profile index")
        with self._lock:
            return self._profiles.get(index)

    def index_of(self, profile):
        profile = profile.profile if isinstance(profile, ProfileSigner) else profile
        with self._lock:
            return profile.index if isinstance(profile, Profile) and self._profiles.get(profile.index) is profile else -1

    def signer(self, profile):
        with self._lock:
            self._alive()
            if self.index_of(profile) < 0:
                raise ValueError("Profile was not generated from this source")
            profile = profile.profile if isinstance(profile, ProfileSigner) else profile
            seed = self._seed_copy()
            material = None
            try:
                material = self._core.generate_profile_keys(seed, profile.index)
                if material[160:] != bytes(profile):
                    raise ValueError("Profile seed does not match its public identity")
                return ProfileSigner._issue(self._core, profile, material[:160])
            finally:
                seed[:] = bytes(len(seed))
                if material is not None:
                    material[:] = bytes(len(material))

    def default_signer(self):
        return self.signer(self.default())

    def next_signer(self):
        return self.signer(self.next())

    def __len__(self):
        with self._lock:
            return len(self._profiles)

    def close(self):
        with self._lock:
            self._closed = True
            if self._seed is not None:
                self._seed[:] = bytes(len(self._seed))
            self._provider = None

    def __enter__(self):
        self._alive()
        return self

    def __exit__(self, *exc):
        self.close()


class Generator:
    """Compatibility generator returning cached signers. Prefer ProfileGenerator.

    close() revokes this facade's cached signers and closes the underlying public
    generator. Parsed/independently issued public identities still cannot sign.
    """
    def __init__(self, seed, core):
        self._public = ProfileGenerator(seed, core)
        self._signers = {}
        self._lock = RLock()
        self._next_index = 1

    def derive(self, index):
        uint(index, 8, "profile index")
        with self._lock:
            self._public._alive()
            if index not in self._signers:
                self._signers[index] = self._public.signer(self._public.derive(index))
            return self._signers[index]

    def default(self):
        return self.derive(0)

    def next(self):
        with self._lock:
            result = self.derive(self._next_index)
            self._next_index += 1
            return result

    def at(self, index):
        uint(index, 8, "profile index")
        with self._lock:
            return self._signers.get(index)

    def index_of(self, profile):
        with self._lock:
            return profile.index if isinstance(profile, ProfileSigner) and self._signers.get(profile.index) is profile else -1

    def __len__(self):
        with self._lock:
            return len(self._signers)

    def close(self):
        with self._lock:
            for signer in self._signers.values():
                signer.dispose()
            self._public.close()

    def __enter__(self):
        self._public._alive()
        return self

    def __exit__(self, *exc):
        self.close()
