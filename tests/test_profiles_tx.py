# SPDX-License-Identifier: MPL-2.0
import json
import pickle
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event
from unittest.mock import patch

from w3sper import (
    AccountBalance, AddressBalance, Bookkeeper, ContractCall, DisposedError, Gas,
    Generator, Profile, ProfileGenerator, ProfileSigner, StakeAmount, StakeInfo,
    TRANSFER, WalletCore,
)

ORACLE = json.loads((Path(__file__).parent / "fixtures/js-oracle.json").read_text())
SEED = bytes.fromhex(ORACLE["seed"])


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = WalletCore()
        cls.addClassCleanup(cls.core.close)

    def test_public_generator_cache_source_and_signer_issuance(self):
        with ProfileGenerator(SEED, self.core) as gen:
            first, public = gen.next(), gen.default()
            self.assertEqual((first.index, public.index), (1, 0))
            self.assertIs(public, gen.default())
            self.assertIs(first, gen.at(1))
            self.assertIsNone(gen.at(2))
            self.assertEqual(len(gen), 2)
            self.assertFalse(hasattr(public, "_seed"))
            self.assertFalse(hasattr(public, "sign_bls_message"))
            with gen.signer(public) as signer, gen.signer(public) as second:
                self.assertIsNot(signer, second)
                self.assertEqual(signer.account, public.account)
                self.assertEqual(gen.index_of(signer), 0)
                self.assertTrue(signer.same_source_of(first))
                signer.dispose()
                self.assertEqual(len(second.sign_bls_message(b"still valid")), 48)
            with self.assertRaises(ValueError):
                gen.signer(Profile(bytes(public)))
            with ProfileGenerator(SEED, self.core) as foreign:
                self.assertFalse(public.same_source_of(foreign.default()))
                self.assertEqual(gen.index_of(foreign.default()), -1)
                with self.assertRaises(ValueError):
                    gen.signer(foreign.default())
            with self.assertRaises(FrozenInstanceError):
                public.data = bytes(160)

    def test_provider_changes_refusal_and_issued_lifetime(self):
        current, reads = [SEED], []
        def provider():
            reads.append(1)
            if isinstance(current[0], Exception):
                raise current[0]
            return current[0]
        with ProfileGenerator(provider, self.core) as gen:
            public = gen.default()
            signer = gen.signer(public)
            self.addCleanup(signer.dispose)
            signature = signer.sign_bls_message(b"abc")
            self.assertEqual(len(reads), 2)
            current[0] = bytes(64)
            with self.assertRaisesRegex(ValueError, "seed does not match"):
                gen.signer(public)
            current[0] = RuntimeError("wallet locked")
            with self.assertRaisesRegex(RuntimeError, "wallet locked"):
                gen.signer(public)
            reads.clear()
            self.assertEqual(signer.sign_bls_message(b"abc"), signature)
            self.assertEqual(reads, [])
        self.assertEqual(signer.sign_bls_message(b"abc"), signature)

    def test_disposal_clears_owned_keys_and_snapshots(self):
        with ProfileGenerator(SEED, self.core) as gen:
            signer = gen.default_signer()
            public = signer.profile
            with self.assertRaises(DisposedError):
                with signer._keys("both") as snapshot:
                    self.assertNotEqual(snapshot, bytes(96))
                    signer.dispose()
                    self.assertEqual(snapshot, bytes(96))
            self.assertEqual(signer._material, bytes(160))
            signer.dispose()
            self.assertEqual(signer.profile, public)
            with self.assertRaises(DisposedError):
                signer.sign_bls_message(b"a")
            with self.assertRaises(TypeError):
                pickle.dumps(signer)

    def test_disposal_rejects_inflight_result(self):
        with ProfileGenerator(SEED, self.core) as gen, gen.default_signer() as signer:
            ready, release = Event(), Event()
            original = self.core.sign_bls_message_with_key
            def blocked(*args):
                result = original(*args)
                ready.set()
                self.assertTrue(release.wait(5))
                return result
            with patch.object(self.core, "sign_bls_message_with_key", blocked), ThreadPoolExecutor(1) as pool:
                task = pool.submit(signer.sign_bls_message, b"abc")
                try:
                    self.assertTrue(ready.wait(5))
                    signer.dispose()
                finally:
                    release.set()
                with self.assertRaises(DisposedError):
                    task.result(5)

    def test_seed_input_snapshot_and_no_seed_on_signer(self):
        seed = bytearray(SEED)
        with Generator(seed, self.core) as gen:
            seed[:] = bytes(64)
            signer = gen.default()
            self.assertEqual(signer.profile.data.hex(), ORACLE["profiles"][0]["bytes"])
            self.assertFalse(hasattr(signer, "_seed"))
            self.assertNotIn(SEED.hex(), repr(signer))
            self.assertIs(signer, gen.default())
            self.assertEqual(gen.index_of(signer.profile), -1)
            with self.assertRaises(AttributeError):
                signer.index = 255
        self.assertTrue(signer.disposed)

    def test_bounds_concurrency_and_retry(self):
        with ProfileGenerator(SEED, self.core) as gen:
            with ThreadPoolExecutor(4) as pool:
                profiles = list(pool.map(lambda _: gen.next(), range(16)))
            self.assertEqual(sorted(p.index for p in profiles), list(range(1, 17)))
            gen._next_index = 255
            self.assertEqual(gen.next().index, 255)
            with self.assertRaises(ValueError):
                gen.next()
            for index in (-1, 256, True, 1.0, "1"):
                for method in (gen.derive, gen.at):
                    with self.assertRaises((TypeError, ValueError)):
                        method(index)
        with ProfileGenerator(SEED, self.core) as gen:
            with patch.object(self.core, "generate_profile", side_effect=RuntimeError("derivation failed")):
                with self.assertRaises(RuntimeError):
                    gen.next()
            self.assertEqual(gen.next().index, 1)
        for seed in (b"", bytes(63), bytes(65), None, 64):
            with self.assertRaises((TypeError, ValueError)):
                ProfileGenerator(seed, self.core)


class BuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = WalletCore()
        cls.addClassCleanup(cls.core.close)

    def setUp(self):
        self.gen = Generator(SEED, self.core)
        self.addCleanup(self.gen.close)
        self.sender, self.receiver, self.owner = self.gen.default(), self.gen.next(), self.gen.next()
        self.lookups = []
        def lookup(key):
            self.lookups.append(key)
            return AccountBalance(5, 10**16)
        self.book = Bookkeeper(self.core, lookup, stake_info=lambda _: StakeInfo())
        self.entry = self.book.bind(self.sender)

    def test_js_all_deterministic_transaction_families(self):
        for vector in ORACLE["transactions"]:
            with self.subTest(vector=vector["name"]):
                spec = vector["input"]
                kind = spec["kind"]
                if kind == "deploy":
                    builder = self.entry.deploy(bytes.fromhex(spec["bytecode"]),
                        init_args=bytes.fromhex(spec["initArgs"]), deploy_nonce=int(spec["deployNonce"]))
                elif kind == "stake":
                    owner = self.gen.derive(spec["ownerIndex"])
                    builder = self.entry.stake(int(spec["value"]), owner_signer=owner)
                else:
                    builder = self.entry.transfer(int(spec.get("value", "0"))).to(
                        self.sender.account if kind == "committed" else self.receiver.account)
                    if "memo" in spec:
                        builder = builder.memo(spec["memo"])
                    if "fnName" in spec:
                        builder = builder.payload(ContractCall(spec["contractId"], spec["fnName"], bytes.fromhex(spec["fnArgs"])))
                    if "deposit" in spec:
                        builder = builder.deposit(int(spec["deposit"]))
                    if kind == "committed":
                        builder = builder.committed()
                result = builder.gas(Gas(500_000_000, 3)).chain(42).build()
                self.assertEqual((result.buffer.hex(), result.hash), (vector["bytes"], vector["hash"]))
                self.assertEqual(result.nonce, 6)
                self.assertEqual(result.contract_id, vector.get("contractId"))

    def test_immutable_routes_and_current_nonce(self):
        base = self.entry.transfer(123).chain(42).nonce(5).gas(Gas(500_000_000, 3))
        first = base.to(self.receiver.account).build()
        second = base.to(self.owner.account).amount(999).build()
        self.assertEqual(first.buffer.hex(), ORACLE["transactions"][0]["bytes"])
        self.assertNotEqual(first.hash, second.hash)
        self.assertEqual(self.lookups, [])
        with self.assertRaises(ValueError):
            base.build()
        with self.assertRaises(FrozenInstanceError):
            base._amount = 0
        with self.assertRaises(ValueError):
            base.to(self.receiver.account).nonce(2**64 - 1).build()

    def test_authority_and_prerequisites(self):
        public_entry = self.book.bind(Profile(bytes(self.sender.profile)))
        self.assertEqual(public_entry.balance(), AccountBalance(5, 10**16))
        with self.assertRaisesRegex(ValueError, "public profiles cannot sign"):
            public_entry.transfer(1).to(self.receiver.account).chain(42).build()
        with self.assertRaises(TypeError):
            public_entry.balance("address")
        with self.assertRaises(ValueError):
            self.entry.transfer(1).to(self.receiver.account).build()
        with self.assertRaisesRegex(ValueError, "account_balance"):
            Bookkeeper(self.core).bind(self.sender).transfer(1).to(self.receiver.account).chain(42).build()
        self.sender.dispose()
        with self.assertRaises(DisposedError):
            self.entry.transfer(1).to(self.receiver.account).chain(42).nonce(0).build()

    def test_strict_builder_bounds(self):
        tx = self.entry.transfer()
        for method in (tx.amount, tx.deposit, tx.nonce):
            for value in (-1, 2**64, True, 1.0, None):
                with self.assertRaises((TypeError, ValueError)):
                    method(value)
        for value in (-1, 256, True, "42"):
            with self.assertRaises((TypeError, ValueError)):
                tx.chain(value)
        for value in ("1111", "not-a-key", b"", None):
            with self.assertRaises((TypeError, ValueError)):
                tx.to(value)
        with self.assertRaises(TypeError):
            tx.gas(None)
        with self.assertRaises(TypeError):
            tx.payload({"fnName": "invented"})
        with self.assertRaises(ValueError):
            tx.amount(2**64 - 1).to(self.receiver.account).chain(42).build()

    def test_committed_call_restrictions(self):
        base = self.entry.transfer(0).chain(42).nonce(0).committed()
        call = ContractCall(TRANSFER, "deposit", bytes(8))
        for tx in (base.to(self.sender.account), base.to(self.sender.account).memo("memo"),
                   base.payload(call).to(self.receiver.account), base.payload(call).to(self.sender.address),
                   base.payload(call).to(self.sender.account).amount(1),
                   base.payload(call).to(self.sender.account).deposit(1)):
            with self.assertRaises(ValueError):
                tx.build()

    def test_phoenix_snapshot_balance_pick_and_deposit_budget(self):
        fixture = ORACLE["notes"][0]
        notes = {bytes.fromhex(k): bytes.fromhex(v) for k, v in fixture["entries"]}
        book = Bookkeeper(self.core, address_notes=lambda _: notes)
        self.assertEqual(book.balance(self.sender), AddressBalance(800_000_000_000_000, 400_000_000_000_000))
        picked = book.pick(self.sender, int(fixture["pickAmount"]))
        self.assertEqual([(k.hex(), v.hex()) for k, v in picked.items()], [tuple(e) for e in fixture["picked"]])
        with self.assertRaisesRegex(ValueError, "insufficient"):
            book.pick(self.sender, 400_000_000_000_001)
        with self.assertRaises(TypeError):
            book.balance(self.sender.address_id)
        with self.assertRaisesRegex(ValueError, "network"):
            book.bind(self.sender).transfer(1).to(self.receiver.address).chain(42).build()

    def test_staking_owner_resolution_and_partial_bounds(self):
        state = [StakeInfo(StakeAmount(10**12 + 123), reward=1000)]
        keys = {"account": self.sender.account_id, "owner": {"Account": self.owner.account_id}}
        entry = Bookkeeper(self.core, lambda _: AccountBalance(0, 10**16),
                           stake_info=lambda _: state[0], stake_keys=lambda _: keys).bind(self.sender)
        for builder in (entry.topup(1), entry.topup(1, owner_signer=self.sender),
                        entry.unstake(124, owner_signer=self.owner), entry.withdraw(1001, owner_signer=self.owner),
                        entry.withdraw(0, owner_signer=self.owner), entry.stake(10**12, owner_signer=self.owner)):
            with self.assertRaises(ValueError):
                builder.chain(42).build()
        for builder in (entry.topup(1, owner_signers=[self.owner]), entry.unstake(123, owner_signer=self.owner),
                        entry.unstake(owner_signer=self.owner), entry.withdraw(123, owner_signer=self.owner)):
            result = builder.chain(42).build()
            self.assertEqual(result.nonce, 1)
            self.assertGreater(len(result.buffer), 293)
        keys["owner"] = {"Contract": TRANSFER}
        with self.assertRaisesRegex(ValueError, "contract-owned"):
            entry.topup(1, owner_signer=self.owner).chain(42).build()
        state[0] = StakeInfo()
        with self.assertRaisesRegex(ValueError, "new stake"):
            entry.stake(10**12, owner_signers=[self.owner]).chain(42).build()

    def test_conversion_and_deployment_bounds_and_snapshots(self):
        result = self.entry.shield(123).nonce(0).chain(42).build()
        self.assertEqual(result.nonce, 1)
        code, init = bytearray(b"\0asm\1\0\0\0"), bytearray(b"\x01")
        builder = self.entry.deploy(code, init_args=init).deploy_nonce(7).account_nonce(0).chain(42)
        code[:], init[:] = bytes(len(code)), bytes(len(init))
        result = builder.build()
        self.assertEqual(result.contract_id, self.core.contract_id(b"\0asm\1\0\0\0", self.sender.account, 7))
        for method in (builder.deploy_nonce, builder.account_nonce):
            for value in (-1, 2**64, True, 1.5):
                with self.assertRaises((TypeError, ValueError)):
                    method(value)
        with self.assertRaises(ValueError):
            self.entry.deploy(b"")


if __name__ == "__main__":
    unittest.main()
