# SPDX-License-Identifier: MPL-2.0
"""Real native Phoenix ABI + proof preverified by the reference node (no mocks)."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from w3sper import Bookkeeper, ContractCall, Memo, ProfileGenerator, WalletCore

FIXTURE = json.loads((Path(__file__).parent / "fixtures/phoenix-native.json").read_text())


class PhoenixNativeTests(unittest.TestCase):
    def setUp(self):
        self.core = WalletCore()
        self.gen = ProfileGenerator(bytes.fromhex(FIXTURE["seed"]), self.core)
        self.sender = self.gen.signer(self.gen.derive(FIXTURE["index"]))
        self.owner = self.gen.signer(self.gen.derive(FIXTURE["owner_index"]))
        self.receiver = self.gen.derive(FIXTURE["receiver_index"])
        self.notes = {bytes.fromhex(k): bytes.fromhex(v) for k, v in FIXTURE["notes"]}
        self.inputs = {"inputs": self.notes, "openings": tuple(bytes.fromhex(o) for o in FIXTURE["openings"]),
                       "root": bytes.fromhex(FIXTURE["root"])}
        self.common = {"chain_id": FIXTURE["chain_id"], "gas_limit": FIXTURE["gas_limit"],
                       "gas_price": FIXTURE["gas_price"], "rng": bytes.fromhex(FIXTURE["rng"])}
        self.addCleanup(self.core.close)
        self.addCleanup(self.gen.close)
        self.addCleanup(self.sender.dispose)
        self.addCleanup(self.owner.dispose)

    def test_exact_live_preverified_proof_roundtrip_and_owned_snapshots(self):
        with self.sender._keys("phoenix") as key:
            tx, circuits = self.core.phoenix_with_key(key, bytes(self.receiver.address), value=FIXTURE["value"],
                data=bytes.fromhex(FIXTURE["data"]), **self.inputs, **self.common)
        self.assertEqual(tx.hex(), FIXTURE["unproven"])
        self.assertEqual(circuits.hex(), FIXTURE["circuits"])
        proof = bytearray.fromhex(FIXTURE["proof"])
        raw, digest = self.core.into_proven(tx, proof)
        self.assertEqual(raw.hex(), FIXTURE["network_bytes"])
        self.assertEqual(digest, FIXTURE["hash"])
        self.assertEqual(raw[0], 0)  # no host length prefix
        proof[:] = bytes(len(proof))
        self.core.generate_profile(bytes(range(64)), 0)
        self.core.close()
        self.assertEqual(raw.hex(), FIXTURE["network_bytes"])

    def test_both_conversions_and_all_public_private_staking_exports(self):
        with self.sender._keys("both") as key, self.owner._keys("bls") as owner:
            public, digest = self.core.shield(key, 123, nonce=1, **self.common)
            self.assertEqual(public[0], 1)
            self.assertEqual(len(digest), 64)
            unproven = self.core.unshield(key, value=123, **self.inputs, **self.common)
            self.assertTrue(all(len(part) > 1000 for part in unproven))
            for action in ("stake", "unstake", "stake_reward"):
                with self.subTest(action=action):
                    value = self.core.globals.minimum_stake if action == "stake" else 123
                    private_keys, public_keys = key + owner, key[:32] + owner
                    try:
                        result = self.core.stake_transaction(action, private_keys, value, **self.inputs, **self.common)
                        self.assertEqual(result, self.core.stake_transaction(action, private_keys, value, **self.inputs, **self.common))
                        self.assertTrue(all(len(part) > 1000 for part in result))
                        public, digest = self.core.stake_transaction(action, public_keys, value, nonce=1, **self.common)
                        self.assertEqual(public[0], 1)
                        self.assertEqual(len(digest), 64)
                    finally:
                        private_keys[:] = bytes(len(private_keys))
                        public_keys[:] = bytes(len(public_keys))

    def test_shape_failures_never_reach_native_abi(self):
        with self.sender._keys("phoenix") as key, patch.object(self.core, "_invoke", side_effect=AssertionError("native called")):
            for invalid in ({"inputs": []}, {"inputs": [bytes(735)]}, {"openings": []},
                            {"openings": [b"\0"]}, {"root": bytes(31)}, {"openings": [None]}):
                with self.subTest(invalid=invalid):
                    with self.assertRaises((ValueError, TypeError)):
                        self.core.phoenix_with_key(key, bytes(self.receiver.address), value=123,
                                                  **(self.inputs | invalid), **self.common)
            with self.assertRaises(TypeError):
                self.core.pick_notes_with_key(bytes(64), [], 1)
            with self.assertRaises(TypeError):
                self.core.unshield(bytes(96), [], [], bytes(32), 1, chain_id=42)

    def test_specialized_builders_reject_options_their_native_abi_cannot_encode(self):
        entry = Bookkeeper(self.core).bind(self.sender)
        builders = (entry.shield(1).memo("lost"), entry.unshield(1).deposit(1),
                    entry.stake(10**12).payload(ContractCall(bytes(32), "f")),
                    entry.stake(10**12).to(self.receiver.account),
                    entry.deploy(b"wasm").memo("lost"), entry.shield(1).shielded(),
                    entry.unshield(1).public(), entry.unshield(1).nonce(0))
        for builder in builders:
            with self.subTest(kind=builder._kind):
                with self.assertRaises(ValueError):
                    builder.chain(42).build()


if __name__ == "__main__":
    unittest.main()
