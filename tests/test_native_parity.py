# SPDX-License-Identifier: MPL-2.0
"""Independent current-JS and Rust fixtures, exercised with the real bundled WASM."""
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from w3sper import (CodeError, ContractCall, DeployData, Memo, Profile, ProfileGenerator,
                    STAKE, TRANSFER, WalletCore)

FIXTURES = Path(__file__).parent / "fixtures"
JS = json.loads((FIXTURES / "js-oracle.json").read_text())


class NativeParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = WalletCore()
        cls.addClassCleanup(cls.core.close)
        cls.generator = ProfileGenerator(bytes.fromhex(JS["seed"]), cls.core)
        cls.addClassCleanup(cls.generator.close)

    def test_six_current_js_profiles_and_raw_accounts(self):
        for vector in JS["profiles"]:
            profile = self.generator.derive(vector["index"])
            self.assertEqual(bytes(profile).hex(), vector["bytes"])
            self.assertEqual(self.core.accounts_into_raw([profile.account])[0].hex(), vector["rawAccount"])
        self.assertEqual(self.core.accounts_into_raw([]), ())

    def test_nine_current_js_exact_byte_messages(self):
        for vector in JS["messages"]:
            with self.generator.signer(self.generator.derive(vector["index"])) as signer:
                data = bytearray.fromhex(vector["message"])
                saved = bytes(data)
                signature = signer.sign_bls_message(memoryview(data))
                self.assertEqual(signature.hex(), vector["signature"])
                self.assertEqual(bytes(data), saved)

    def test_five_independent_rust_bls_vectors(self):
        count = 0
        for line in (FIXTURES / "bls-message-signing.txt").read_text().splitlines():
            if not line[:1].isdigit():
                continue
            index, seed, message, public, signature = line.split(" ")
            with ProfileGenerator(bytes.fromhex(seed), self.core) as gen:
                with gen.signer(gen.derive(int(index))) as signer:
                    self.assertEqual(signer.account.hex(), public)
                    self.assertEqual(signer.sign_bls_message(bytes.fromhex(message)).hex(), signature)
            count += 1
        self.assertEqual(count, 5)

    def test_current_js_note_maps_balances_selection_bookmarks(self):
        for vector in JS["notes"]:
            notes = {bytes.fromhex(k): bytes.fromhex(v) for k, v in vector["entries"]}
            with self.generator.signer(self.generator.derive(vector["index"])) as signer:
                with signer._keys("view") as key:
                    value, spendable = self.core.balance_with_key(key, notes)
                    self.assertEqual((value, spendable), (int(vector["balance"]["value"]), int(vector["balance"]["spendable"])))
                    selected = self.core.pick_notes_with_key(key, notes, int(vector["pickAmount"]))
                self.assertEqual([(k.hex(), v.hex()) for k, v in selected.items()], [tuple(row) for row in vector["picked"]])
                with signer._keys("phoenix") as key:
                    mapped, info = self.core.map_owned_with_keys(key, b"".join(notes.values()))
                    self.assertEqual(mapped, (notes,))
                bookmarks = self.core.bookmarks(notes)
                self.assertEqual(b"".join(n.to_bytes(8, "little") for n in bookmarks).hex(), vector["bookmarks"])
                self.assertEqual(info, {"block_height": 0, "bookmark": max(bookmarks)})
        self.assertEqual(self.core.bookmarks({}), ())

    def test_multiowner_mapping_and_empty_vectors(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            signers = [stack.enter_context(self.generator.signer(self.generator.derive(i))) for i in range(4)]
            keys = bytearray().join(stack.enter_context(s._keys("phoenix")) for s in signers)
            try:
                notes = [{bytes.fromhex(k): bytes.fromhex(v) for k, v in row["entries"]} for row in JS["notes"]]
                leaves = b"".join(value for group in notes for value in group.values())
                result, info = self.core.map_owned_with_keys(keys, leaves)
                self.assertEqual(result, (*notes, {}))
                self.assertEqual(info["bookmark"], 23)
                self.assertEqual(self.core.map_owned_with_keys(keys, b"")[0], ({}, {}, {}, {}))
            finally:
                keys[:] = bytes(len(keys))
            with signers[0]._keys("view") as key:
                self.assertEqual(self.core.balance_with_key(key, {}), (0, 0))
                self.assertEqual(self.core.pick_notes_with_key(key, {}, 0), {})

    def test_payload_snapshots_and_allocation_ownership(self):
        core = self.core
        with self.generator.default_signer() as signer:
            for payload in (Memo("🌒"), Memo(b""), ContractCall(TRANSFER, "deposit", bytes(8)),
                            ContractCall(TRANSFER, "deposit", bytes(8), True),
                            DeployData(b"\0asm\1\0\0\0", signer.account, b""),
                            DeployData(b"\0asm\1\0\0\0", signer.account, None)):
                with self.subTest(payload=type(payload).__name__):
                    exports = dict(core._exports)
                    allocated, freed = [], []
                    def malloc(store, size):
                        ptr = exports["malloc"](store, size)
                        allocated.append((ptr, size))
                        return ptr
                    def free(store, ptr, size):
                        freed.append((ptr, size))
                        return exports["free"](store, ptr, size)
                    with patch.object(core, "_exports", {**exports, "malloc": malloc, "free": free}):
                        result = core.serialize_payload(payload)
                    for allocation in allocated:
                        self.assertEqual(freed.count(allocation), 1)
                    extra = [(p, s) for p, s in freed if (p, s) not in allocated]
                    self.assertEqual(len(extra), 1)
                    self.assertEqual(extra[0][1], len(result) + 4)
                    saved = bytes(result)
                    with signer._keys("bls") as key:
                        first = core.moonlight_with_key(key, None, 0, nonce=1, chain_id=42, data=result)
                    self.assertEqual(result, saved)
                    self.assertEqual(len(first[1]), 64)

    def test_key_issuance_output_wiped_and_signing_status_recovery(self):
        with WalletCore() as core:
            exports = dict(core._exports)
            cleared = []
            def free(store, pointer, size):
                if size in (320, 64):
                    cleared.append((size, core._read(pointer, size)))
                return exports["free"](store, pointer, size)
            with patch.object(core, "_exports", {**exports, "free": free}):
                material = core.generate_profile_keys(bytes(range(64)), 0)
            self.assertCountEqual(cleared, [(64, bytes(64)), (320, bytes(320))])
            material[:] = bytes(len(material))
            with self.assertRaises(CodeError):
                core.sign_bls_message_with_key(bytes(32), b"message")
            self.assertEqual(len(core.generate_profile(bytes(64))), 160)

    def test_native_boundaries_before_allocation(self):
        with patch.object(self.core, "_box", side_effect=AssertionError("allocated")):
            for keys in (b"", bytes(63), bytes(65), bytes(257 * 64)):
                with self.assertRaises(ValueError):
                    self.core.map_owned_with_keys(keys, b"")
            with self.assertRaises(ValueError):
                self.core.map_owned_with_keys(bytes(64), bytes(735))
            for key in (b"", bytes(31), bytes(33)):
                with self.assertRaises(ValueError):
                    self.core.sign_bls_message_with_key(key, b"")
            with self.assertRaises(ValueError):
                self.core.phoenix_with_key(bytes(64), bytes(64), [], [], bytes(32), 0, chain_id=42)
            with self.assertRaises(ValueError):
                self.core.shield(bytes(96), 1, nonce=1, chain_id=42, gas_limit=0)
            with self.assertRaises(ValueError):
                self.core.stake_transaction("unknown", bytes(64), 1, nonce=1, chain_id=42)

    def test_serialized_payload_input_validation(self):
        for value in (None, {}, b"raw"):
            with self.assertRaises(TypeError):
                self.core.serialize_payload(value)
        for name in ("", None, "a\0b"):
            with self.assertRaises(ValueError):
                ContractCall(TRANSFER, name)
        for contract in (b"", bytes(31), bytes(33), "xyz"):
            with self.assertRaises(ValueError):
                ContractCall(contract, "go")
        with self.assertRaises(ValueError):
            DeployData(b"wasm", b"owner", nonce=2**64)


if __name__ == "__main__":
    unittest.main()
