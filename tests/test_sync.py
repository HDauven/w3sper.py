# SPDX-License-Identifier: MPL-2.0
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import struct
import threading
import time
import unittest

from w3sper import (AccountBalance, AccountSyncer, AddressSyncer, Bookmark, CancellationToken,
                    CancelledError, DisposedError, Network, NetworkError, ProfileGenerator,
                    StakeAmount, StakeInfo, WalletCore)
from w3sper import _rkyv

FIXTURE = json.loads((Path(__file__).parent / "fixtures/js-oracle.json").read_text())
NOTES = [{bytes.fromhex(k): bytes.fromhex(v) for k, v in row["entries"]} for row in FIXTURE["notes"]]


class SyncHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.path, body, dict(self.headers)))
        topic = self.path.rsplit("/", 1)[-1]
        if topic == "num_notes":
            data = (24).to_bytes(8, "little")
        elif topic == "graphql":
            data = json.dumps({"data": self.server.graphql(json.loads(body)["query"])}).encode()
        elif topic.startswith("leaves_from_"):
            data = self.server.leaves
        elif topic == "account":
            data = struct.pack("<QQ", 9, 2**53 + 1)
        elif topic == "get_stake":
            data = b"\1" + bytes(7) + b"\1" + bytes(7) + struct.pack("<QQQ", 10**12, 7, 21) + struct.pack("<QBB", 123, 2, 3) + bytes(6)
        elif topic == "root":
            data = bytes(32)
        elif topic == "opening":
            data = self.server.opening
        elif topic == "existing_nullifiers":
            values = _rkyv.vector_items(body, 32)
            data = _rkyv.vector(values[:1], 32)
        else:
            data = b""
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            if topic.startswith("leaves_from_"):
                for offset in range(0, len(data), 113):
                    self.wfile.write(data[offset:offset + 113])
                    self.wfile.flush()
                    if self.server.hold:
                        self.server.started.set()
                        self.server.release.wait(5)
            else:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.core = WalletCore()
        self.gen = ProfileGenerator(bytes.fromhex(FIXTURE["seed"]), self.core)
        self.signers = [self.gen.signer(self.gen.derive(i)) for i in range(3)]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SyncHandler)
        self.server.requests = []
        self.server.graphql = lambda _: {"block": {"header": {"height": 42}}}
        self.server.leaves = b"".join(n for group in NOTES for n in group.values())
        self.server.opening = b"\1" + bytes(7)
        self.server.hold = False
        self.server.started, self.server.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.network = Network(f"http://127.0.0.1:{self.server.server_port}", core=self.core)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.release.set()
        self.network.close()
        for signer in self.signers:
            signer.dispose()
        self.gen.close()
        self.core.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_actual_owned_mapping_across_http_chunk_boundaries(self):
        syncer = AddressSyncer(self.network, chunk_size=3)
        progress, groups = [], [{}, {}, {}]
        stop = syncer.on_progress(progress.append)
        with syncer.notes(self.signers, from_=Bookmark.from_int(0)) as stream:
            for owned, info in stream:
                for dest, source in zip(groups, owned):
                    dest.update(source)
        self.assertEqual(groups, NOTES)
        self.assertEqual(info, {"block_height": 0, "bookmark": 23})
        self.assertEqual(sum(p["owned_count"] for p in progress), 24)
        self.assertEqual(progress[-1]["progress"], 1)
        stop()
        self.assertEqual(self.server.requests[2][0].rsplit("/", 1)[-1], "leaves_from_pos")
        self.assertEqual(self.server.requests[2][2]["Rusk-feeder"], "true")
        self.assertTrue(stream._released)
        self.assertTrue(all(not s._snapshots for s in self.signers))

    def test_truncated_note_is_rejected_not_silently_discarded(self):
        self.server.leaves = self.server.leaves[:-1]
        with AddressSyncer(self.network, chunk_size=2).notes(self.signers) as stream:
            with self.assertRaisesRegex(NetworkError, "truncated Phoenix"):
                list(stream)
        self.assertTrue(stream._released)
        self.assertTrue(all(not s._snapshots for s in self.signers))

    def test_paused_stream_cancellation_and_signer_disposal_release_secrets(self):
        for dispose in (False, True):
            with self.subTest(dispose=dispose):
                # A fresh signer in each case; ownership source remains common.
                with self.gen.signer(self.gen.default()) as signer:
                    token = CancellationToken()
                    stream = AddressSyncer(self.network, chunk_size=1).notes([signer], cancel=token, timeout=0)
                    self.assertTrue(signer._snapshots)
                    next(stream)
                    if dispose:
                        signer.dispose()
                    else:
                        token.cancel()
                    self.assertTrue(stream._released)
                    self.assertFalse(signer._snapshots)
                    with self.assertRaises(DisposedError if dispose else CancelledError):
                        next(stream)
                    stream.close()
                    self.assertFalse(token._callbacks)

    def test_paused_stream_deadline_and_early_close(self):
        stream = AddressSyncer(self.network).notes(self.signers, timeout=0.15)
        time.sleep(0.25)
        self.assertTrue(stream._released)
        self.assertTrue(all(not s._snapshots for s in self.signers))
        with self.assertRaises(TimeoutError):
            next(stream)
        with AddressSyncer(self.network).notes(self.signers, timeout=0) as stream:
            self.assertTrue(self.signers[0]._snapshots)
        self.assertFalse(self.signers[0]._snapshots)

    def test_openings_root_spent_batches_and_exact_account_state(self):
        addresses = AddressSyncer(self.network)
        self.assertEqual(addresses.root(), bytes(32))
        self.assertEqual(addresses.openings(NOTES[0]), (self.server.opening,) * 8)
        body = [body for path, body, _ in self.server.requests if path.endswith("/opening")]
        self.assertEqual(body, [i.to_bytes(8, "little") for i in range(8)])
        nullifiers = [i.to_bytes(32, "little") for i in range(4000)]
        result = addresses.spent(nullifiers)
        self.assertEqual(result, (nullifiers[0], nullifiers[1919], nullifiers[3838]))
        accounts = AccountSyncer(self.network)
        self.assertEqual(accounts.balances(self.signers), (AccountBalance(9, 2**53 + 1),) * 3)
        self.assertEqual(accounts.stakes([self.signers[0]])[0], StakeInfo(StakeAmount(10**12, 7, 21), 123, 2, 3))
        self.assertEqual(addresses.openings([]), ())
        self.assertEqual(addresses.spent([]), ())
        with self.assertRaises(ValueError):
            addresses.spent([bytes(31)])
        with self.assertRaises(TypeError):
            addresses.notes([self.signers[0].profile])

    def test_enriched_history_values_and_stream_cancellation(self):
        sender = self.signers[0].account_id
        receiver = self.signers[1].account_id
        tx_hash = "ab" * 32
        def graphql(query):
            if "fullMoonlightHistory" in query:
                return {"fullMoonlightHistory": {"json": [{"origin": tx_hash, "block_height": 42,
                    "events": [{"target": "01" + "00" * 31, "topic": "moonlight",
                                "data": {"sender": sender, "receiver": receiver, "value": "9007199254740993"}}]}]}}
            if "tx0:" in query:
                return {"tx0": {"blockHash": "cd" * 32, "blockTimestamp": 100, "err": None,
                                  "gasSpent": 123, "tx": {"gasLimit": 500000000, "gasPrice": 3,
                                  "isDeploy": False, "memo": "hello", "callData": None}}}
            return {"block": {"header": {"height": 42}}}
        self.server.graphql = graphql
        syncer = AccountSyncer(self.network)
        with syncer.history(self.signers[0], from_height=5) as stream:
            result, = list(stream)
        self.assertEqual(result["value"], -9007199254740993)
        self.assertEqual(result["fee_paid"], 369)
        self.assertEqual(result["block_timestamp"], 100000)
        self.assertEqual(result["hash"], tx_hash)
        self.assertTrue(result["success"])
        token = CancellationToken()
        stream = syncer.history(self.signers[0], cancel=token)
        count = len(self.server.requests)
        token.cancel()
        self.assertTrue(stream._released)
        with self.assertRaises(CancelledError):
            next(stream)
        self.assertEqual(len(self.server.requests), count)


if __name__ == "__main__":
    unittest.main()
