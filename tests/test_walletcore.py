# SPDX-License-Identifier: MPL-2.0
import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from threading import Event
from unittest.mock import patch

import wasmtime

from w3sper import AccountBalance, Bookkeeper, ClosedError, CodeError, Generator, Profile, WalletCore, WalletCoreError
from w3sper.walletcore import WASM_RESOURCE, WASM_SHA256

FIXTURE = json.loads((Path(__file__).parent / "fixtures/go-vectors.json").read_text())
SEED = bytes.fromhex(FIXTURE["seed"])
PROFILES = {p["index"]: p for p in FIXTURE["profiles"]}


def inputs(vector):
    receiver = vector["receiver_index"]
    return dict(seed=SEED, sender_index=vector["sender_index"],
                receiver=None if receiver < 0 else bytes.fromhex(PROFILES[receiver]["bytes"])[64:],
                value=int(vector["value"]), nonce=int(vector["nonce"]), chain_id=vector["chain_id"],
                deposit=int(vector["deposit"]), gas_limit=int(vector["gas_limit"]), gas_price=int(vector["gas_price"]))


class RealWalletCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = WalletCore()
        cls.addClassCleanup(cls.core.close)

    def test_bundled_artifact_and_globals(self):
        digest = hashlib.sha256(files("w3sper").joinpath(WASM_RESOURCE).read_bytes()).hexdigest()
        self.assertEqual(digest, FIXTURE["provenance"]["wasm_sha256"])
        self.assertEqual(digest, WASM_SHA256)
        self.assertEqual(asdict(self.core.globals), FIXTURE["globals"])

    def test_go_profile_vectors(self):
        for index, expected in PROFILES.items():
            with self.subTest(index=index):
                profile = Profile(self.core.generate_profile(SEED, index))
                self.assertEqual(bytes(profile).hex(), expected["bytes"])
                self.assertEqual(profile.account_id, expected["account"])
                self.assertEqual(profile.address_id, expected["address"])

    def test_go_signed_transfer_vectors(self):
        for vector in FIXTURE["transfers"]:
            with self.subTest(vector=vector["name"]):
                buffer, tx_hash = self.core.moonlight(**inputs(vector))
                self.assertEqual(buffer.hex(), vector["bytes"])
                self.assertEqual(tx_hash, vector["hash"])

    def test_real_bookkeeper_end_to_end(self):
        generator = Generator(SEED, self.core)
        sender, receiver = generator.default(), generator.next()
        book = Bookkeeper(self.core, lambda _: AccountBalance(5, 1_000_000_000_000))
        result = (book.bind(sender).transfer().amount(1_000_000_000).chain(42)
                  .to(receiver.profile.account_id).build())
        expected = FIXTURE["transfers"][1]
        self.assertEqual(result.buffer.hex(), expected["bytes"])
        self.assertEqual(result.hash, expected["hash"])
        self.assertEqual((result.nonce, result.chain_id), (6, 42))
        explicit = (book.bind(sender).transfer().amount(1_000_000_000).chain(42).nonce(5)
                    .to(receiver.profile.account_id).build())
        self.assertEqual(result, explicit)

    def test_outputs_survive_reuse_and_close(self):
        with WalletCore() as core:
            profile = core.generate_profile(SEED)
            signed = core.moonlight(**inputs(FIXTURE["transfers"][0]))
            for _ in range(8):
                core.generate_profile(bytes(64), 255)
                core.moonlight(**inputs(FIXTURE["transfers"][2]))
        self.assertEqual(profile.hex(), PROFILES[0]["bytes"])
        self.assertEqual(signed[0].hex(), FIXTURE["transfers"][0]["bytes"])
        self.assertEqual(signed[1], FIXTURE["transfers"][0]["hash"])
        self.assertIsInstance(profile, bytes)
        self.assertIsInstance(signed[0], bytes)

    def test_mutable_inputs_are_copied(self):
        seed = bytearray(SEED)
        profile = Generator(seed, self.core).default()
        seed[:] = bytes(64)
        result = (Bookkeeper(self.core).bind(profile).transfer().amount(1_000_000_000)
                  .chain(42).nonce(0).to(PROFILES[1]["account"]).build())
        self.assertEqual(result.buffer.hex(), FIXTURE["transfers"][0]["bytes"])

    def test_bad_inputs_never_reach_wasm(self):
        with patch.object(self.core, "_box", side_effect=AssertionError("WASM allocated before validation")):
            for seed in (b"", bytes(63), bytes(65), 64, None, "secret"):
                with self.subTest(seed_type=type(seed)), self.assertRaises((TypeError, ValueError)):
                    self.core.generate_profile(seed)
            for index in (-1, 256, 2**64, True, 1.0, "0"):
                with self.subTest(index=index), self.assertRaises((TypeError, ValueError)):
                    self.core.generate_profile(SEED, index)
            changes = {"seed": (bytes(63), None), "sender_index": (-1, 256, True),
                       "receiver": (b"", bytes(64), bytes(95), bytes(97), 96),
                       "chain_id": (-1, 256, 42.0, True, "42")}
            for field in ("value", "deposit", "gas_limit", "gas_price", "nonce"):
                changes[field] = (-1, 2**64, True, 1.0, None, "1")
            for field, values in changes.items():
                for value in values:
                    args = inputs(FIXTURE["transfers"][0])
                    args[field] = value
                    with self.subTest(field=field, type=type(value)), self.assertRaises((TypeError, ValueError)):
                        self.core.moonlight(**args)

    def test_real_typed_status_and_recovery(self):
        args = inputs(FIXTURE["transfers"][0])
        args["receiver"] = bytes(96)  # right length, not a serialized BLS point
        for _ in range(3):
            with self.assertRaises(CodeError) as caught:
                self.core.moonlight(**args)
            self.assertEqual(caught.exception.code, FIXTURE["invalid_account_status"])
            self.assertEqual(caught.exception.operation, "moonlight")
        self.assertEqual(self.core.generate_profile(SEED), bytes.fromhex(PROFILES[0]["bytes"]))
        self.assertNotIn(SEED.hex(), str(caught.exception))
        self.assertNotIn(repr(SEED), str(caught.exception))

    def test_allocation_sizes_and_seed_wiping(self):
        with WalletCore() as core:
            exports = dict(core._exports)
            allocated, freed, wiped = [], [], []
            def malloc(store, size):
                pointer = exports["malloc"](store, size)
                allocated.append((pointer, size))
                return pointer
            def free(store, pointer, size):
                freed.append((pointer, size))
                if size == 64 and (pointer, size) == allocated[0]:
                    wiped.append(core._read(pointer, size))
                return exports["free"](store, pointer, size)
            overridden = {**exports, "malloc": malloc, "free": free}
            with patch.object(core, "_exports", overridden):
                buffer, _ = core.moonlight(**inputs(FIXTURE["transfers"][0]))
            for allocation in allocated:
                self.assertEqual(freed.count(allocation), 1)
            self.assertEqual(len(freed), len(allocated) + 1)
            self.assertEqual([size for ptr, size in freed if (ptr, size) not in allocated], [len(buffer) + 4])
            self.assertEqual(wiped, [bytes(64)])
            allocated.clear()
            freed.clear()
            wiped.clear()
            args = inputs(FIXTURE["transfers"][0])
            args["receiver"] = bytes(96)
            with patch.object(core, "_exports", overridden), self.assertRaises(CodeError):
                core.moonlight(**args)
            self.assertCountEqual(allocated, freed)
            self.assertEqual(wiped, [bytes(64)])

    def test_memory_bounds_and_null_output(self):
        with WalletCore() as core:
            for pointer, size in ((-1, 1), (0, -1), (core._memory.data_len(core._store), 1)):
                with self.assertRaises(WalletCoreError):
                    core._read(pointer, size)
            with core._operation() as allocations:
                slot = core._box(allocations, bytes(4))
                with self.assertRaisesRegex(WalletCoreError, "null output"):
                    core._take_output(allocations, slot)
                slot = core._box(allocations, b"\xff" * 4)
                with self.assertRaisesRegex(WalletCoreError, "out of bounds"):
                    core._take_output(allocations, slot)

    def test_missing_status_and_unknown_code(self):
        for code, error in ((None, WalletCoreError), (123, CodeError)):
            with self.subTest(code=code), WalletCore() as core:
                exports = dict(core._exports)
                with patch.object(core, "_exports", {**exports, "generate_profile": lambda *args: code}):
                    with self.assertRaises(error) as caught:
                        core.generate_profile(SEED)
                self.assertIs(type(caught.exception), error)
                if code is None:
                    with self.assertRaises(ClosedError):
                        core.generate_profile(SEED)
                else:
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(core.generate_profile(SEED).hex(), PROFILES[0]["bytes"])

    def test_trap_closes_instance(self):
        with WalletCore() as core:
            def trap(*args):
                raise wasmtime.Trap("test trap")
            with patch.object(core, "_invoke", trap):
                with self.assertRaises(WalletCoreError):
                    core.generate_profile(SEED)
            with self.assertRaises(ClosedError):
                core.generate_profile(SEED)

    def test_invalid_output_closes_instance(self):
        with WalletCore() as core:
            with patch.object(core, "_take_output", side_effect=WalletCoreError("invalid output")):
                with self.assertRaises(WalletCoreError):
                    core.moonlight(**inputs(FIXTURE["transfers"][0]))
            # An untrustworthy output length must not leave a partly owned heap alive.
            with self.assertRaises(ClosedError):
                core.generate_profile(SEED)

    def test_close_is_idempotent(self):
        core = WalletCore()
        core.close()
        core.close()
        for operation in (lambda: core.generate_profile(SEED),
                          lambda: core.moonlight(**inputs(FIXTURE["transfers"][0])),
                          core.__enter__):
            with self.assertRaises(ClosedError):
                operation()
        self.assertEqual(asdict(core.globals), FIXTURE["globals"])

    def test_concurrent_real_calls(self):
        def work(i):
            vector = FIXTURE["transfers"][i % len(FIXTURE["transfers"])]
            profile = self.core.generate_profile(SEED, 255)
            signed = self.core.moonlight(**inputs(vector))
            self.assertEqual(profile.hex(), PROFILES[255]["bytes"])
            self.assertEqual(signed, (bytes.fromhex(vector["bytes"]), vector["hash"]))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, range(16)))

    def test_close_waits_for_operation(self):
        core = WalletCore()
        self.addCleanup(core.close)
        entered, release, closed = Event(), Event(), Event()
        invoke = core._invoke
        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")
            return invoke(*args)
        def close():
            core.close()
            closed.set()
        with patch.object(core, "_invoke", blocked), ThreadPoolExecutor(max_workers=2) as pool:
            deriving = pool.submit(core.generate_profile, SEED)
            try:
                self.assertTrue(entered.wait(5))
                closing = pool.submit(close)
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
            self.assertEqual(deriving.result(timeout=5).hex(), PROFILES[0]["bytes"])
            closing.result(timeout=5)
        self.assertTrue(closed.is_set())
        with self.assertRaises(ClosedError):
            core.generate_profile(SEED)


if __name__ == "__main__":
    unittest.main()
