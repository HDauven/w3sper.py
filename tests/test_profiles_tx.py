# SPDX-License-Identifier: MPL-2.0
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

from w3sper import AccountBalance, Bookkeeper, Gas, Generator, Profile, SigningProfile


class FakeCore:
    """Orchestration tests only; test_walletcore.py always uses the real WASM."""

    def __init__(self):
        self.generated = []
        self.signed = []

    def generate_profile(self, seed, index):
        self.generated.append(index)
        return bytes([index]) * 160

    def moonlight(self, seed, sender_index, receiver, value, **kwargs):
        self.signed.append(dict(index=sender_index, receiver=receiver, value=value, **kwargs))
        return b"test-only", "test-only"


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.core = FakeCore()
        self.gen = Generator(bytes(64), self.core)

    def test_call_order_and_cache(self):
        first = self.gen.next()
        default = self.gen.default()
        self.assertEqual(first.index, 1)
        self.assertEqual(default.index, 0)
        self.assertIs(default, self.gen.default())
        self.assertIs(first, self.gen.at(1))
        self.assertIsNone(self.gen.at(2))
        self.assertEqual(len(self.gen), 2)
        self.assertEqual(self.core.generated, [1, 0])
        self.assertEqual(self.gen.next().index, 2)
        other = Generator(bytes(64), self.core)
        self.assertEqual(other.default().index, 0)
        self.assertEqual(other.next().index, 1)

    def test_source_identity(self):
        own = self.gen.default()
        foreign = Generator(bytes(64), self.core).default()
        self.assertEqual(self.gen.index_of(own), 0)
        self.assertEqual(self.gen.index_of(foreign), -1)
        self.assertEqual(self.gen.index_of(own.profile), -1)
        self.assertEqual(self.gen.index_of(None), -1)

    def test_exhaustion_and_explicit_derivation(self):
        last = self.gen.derive(255)
        for index in range(1, 256):
            self.assertEqual(self.gen.next().index, index)
        self.assertIs(last, self.gen.at(255))
        with self.assertRaises(ValueError):
            self.gen.next()
        self.assertEqual(self.gen.default().index, 0)
        self.assertEqual(len(self.gen), 256)
        for index in (-1, 256, 2**80, True, "1", 1.0):
            for method in (self.gen.derive, self.gen.at):
                with self.assertRaises((ValueError, TypeError)):
                    method(index)

    def test_failed_derivation_does_not_advance(self):
        class BrokenCore(FakeCore):
            def generate_profile(self, seed, index):
                if not self.generated:
                    self.generated.append(index)
                    raise RuntimeError("derivation failed")
                return super().generate_profile(seed, index)
        gen = Generator(bytes(64), BrokenCore())
        with self.assertRaises(RuntimeError):
            gen.next()
        self.assertEqual(gen.next().index, 1)

    def test_concurrent_generator(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            profiles = list(pool.map(lambda _: self.gen.next(), range(32)))
        self.assertEqual(sorted(p.index for p in profiles), list(range(1, 33)))
        self.assertEqual(len(self.gen), 32)

    def test_seed_boundaries_and_repr(self):
        for seed in (bytes(63), bytes(65), b""):
            with self.assertRaises(ValueError):
                Generator(seed, self.core)
            with self.assertRaises(ValueError):
                SigningProfile(self.core, seed)
        with self.assertRaises(TypeError):
            Generator(64, self.core)
        seed = bytearray(range(64))
        signer = Generator(seed, self.core).default()
        seed[:] = bytes(64)
        self.assertEqual(signer._seed, bytes(range(64)))
        for obj in (signer, self.gen, Bookkeeper(self.core).bind(signer).transfer()):
            self.assertNotIn(repr(signer._seed), repr(obj))
            self.assertNotIn(signer._seed.hex(), repr(obj))
        with self.assertRaises(FrozenInstanceError):
            signer.index = 255


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.core = FakeCore()
        self.gen = Generator(bytes(64), self.core)
        self.sender = self.gen.default()
        self.receiver = self.gen.next().profile
        self.lookups = []
        def lookup(key):
            self.lookups.append(key)
            return AccountBalance(nonce=7, value=100)
        self.book = Bookkeeper(self.core, lookup)
        self.entry = self.book.bind(self.sender)

    def test_callback_nonce_resolution(self):
        result = self.entry.transfer().amount(5).chain(42).to(self.receiver.account_id).build()
        self.assertEqual((result.nonce, result.chain_id), (8, 42))
        self.assertEqual(self.lookups, [self.sender.profile.account])
        self.assertEqual(self.core.signed[0]["nonce"], 8)
        self.assertEqual(self.core.signed[0]["receiver"], self.receiver.account)

    def test_explicit_nonce_and_options(self):
        result = (self.entry.transfer().amount(9).deposit(3).gas(Gas(4000, 2))
                  .chain(0).nonce(0).to(self.receiver.account_id).build())
        self.assertEqual(result.nonce, 1)
        self.assertFalse(self.lookups)
        self.assertEqual(self.core.signed[0], dict(index=0, receiver=self.receiver.account, value=9,
                         nonce=1, chain_id=0, deposit=3, gas_limit=4000, gas_price=2))

    def test_independent_routes_and_amounts(self):
        base = self.entry.transfer().amount(5).chain(42).nonce(0)
        first = base.to(self.receiver.account_id)
        other = self.gen.next().profile
        second = base.to(other.account_id).amount(17)
        first.build()
        second.build()
        self.assertEqual([t["receiver"] for t in self.core.signed], [self.receiver.account, other.account])
        self.assertEqual([t["value"] for t in self.core.signed], [5, 17])
        with self.assertRaises(ValueError):
            base.build()

    def test_public_profiles_never_sign(self):
        public = Profile(bytes(self.sender.profile))
        entry = self.book.bind(public)
        self.assertEqual(entry.balance().value, 100)
        with self.assertRaisesRegex(ValueError, "public profiles cannot sign"):
            entry.transfer().chain(42).nonce(0).to(self.receiver.account_id).build()
        self.assertFalse(self.core.signed)

    def test_missing_requirements(self):
        transfer = self.entry.transfer().to(self.receiver.account_id)
        with self.assertRaisesRegex(ValueError, "chain id"):
            transfer.build()
        transfer = Bookkeeper(self.core).bind(self.sender).transfer().chain(42).to(self.receiver.account_id)
        with self.assertRaisesRegex(ValueError, "account_balance"):
            transfer.build()
        transfer = Bookkeeper(None).bind(self.sender).transfer().chain(42).nonce(0).to(self.receiver.account_id)
        with self.assertRaisesRegex(ValueError, "WalletCore"):
            transfer.build()
        with self.assertRaises(TypeError):
            self.book.bind(None)

    def test_rejects_invalid_values(self):
        transfer = self.entry.transfer()
        for method in (transfer.amount, transfer.deposit, transfer.nonce):
            for value in (-1, 2**64, True, 1.0, None):
                with self.assertRaises((TypeError, ValueError)):
                    method(value)
        for value in (-1, 256, True, "42"):
            with self.assertRaises((TypeError, ValueError)):
                transfer.chain(value)
        with self.assertRaises(ValueError):
            transfer.gas(Gas(2**64, 1))
        with self.assertRaises(TypeError):
            transfer.gas(None)
        with self.assertRaises(ValueError):
            transfer.to("1111")
        with self.assertRaises(ValueError):
            transfer.to("not-a-key")

    def test_nonce_increment_overflow(self):
        with self.assertRaises(ValueError):
            self.entry.transfer().chain(42).nonce(2**64 - 1).to(self.receiver.account_id).build()
        self.assertFalse(self.core.signed)

    def test_balances_and_unavailable_phoenix(self):
        self.assertEqual(self.book.balance(self.receiver.account_id), AccountBalance(7, 100))
        with self.assertRaises(NotImplementedError):
            self.book.balance(self.receiver.address_id)
        with self.assertRaises(NotImplementedError):
            self.entry.transfer().to(self.receiver.address_id)
        with self.assertRaises(ValueError):
            self.book.balance("123")
        with self.assertRaises(ValueError):
            Bookkeeper(self.core).balance(self.receiver.account_id)
        with self.assertRaises(TypeError):
            Bookkeeper(self.core, lambda _: {"nonce": 1}).balance(self.receiver.account_id)
        for nonce, value in ((-1, 0), (0, -1), (2**64, 0), (False, 0), (0, 1.0)):
            with self.assertRaises((TypeError, ValueError)):
                AccountBalance(nonce, value)


if __name__ == "__main__":
    unittest.main()
