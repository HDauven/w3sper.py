# SPDX-License-Identifier: MPL-2.0
"""Real local HTTP/TLS/WS transports; no simulated transaction success/crypto."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed

from w3sper import CancellationToken, CancelledError, Contract, Network, NetworkError, STAKE, TRANSFER
from w3sper.cancellation import Budget
from w3sper.rues import RuesEvent
from w3sper.transport import request


class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_call(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, dict(self.headers)))
        self.server.started.set()
        if self.server.mode == "headers":
            self.server.release.wait(5)
        if self.server.hook:
            self.server.hook(self)
        payload = b'{"data":{"answer":42}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            if self.server.mode == "body":
                self.wfile.write(payload[:1])
                self.wfile.flush()
                self.server.release.wait(5)
                self.wfile.write(payload[1:])
            else:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_POST = handle_call
    do_GET = handle_call
    do_DELETE = handle_call


class HTTPTestCase(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler)
        self.server.mode = "normal"
        self.server.release, self.server.started = threading.Event(), threading.Event()
        self.server.requests = []
        self.server.hook = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.network = Network(self.url)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.release.set()
        self.network.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class DeadlineTests(HTTPTestCase):
    def test_preaborted_budget_constructor_does_not_leak_timer_or_callback(self):
        token = CancellationToken()
        token.cancel()
        before = set(threading.enumerate())
        for _ in range(20):
            with self.assertRaises(CancelledError):
                Budget(120, token)
        self.assertEqual(set(threading.enumerate()), before)
        self.assertEqual(token._callbacks, set())
        with self.assertRaises(CancelledError):
            self.network.query("answer", cancel=token)
        self.assertEqual(self.server.requests, [])

    def test_request_constructor_failures_release_all_timed_resources(self):
        before = set(threading.enumerate())
        for url in ("http://[broken", "http://localhost:99999", "not-a-url", "ftp://localhost", "http://user:secret@localhost"):
            with self.assertRaises(ValueError):
                request(url, timeout=60)
        with patch("w3sper.transport.http.client.HTTPConnection", side_effect=RuntimeError("constructor")):
            with self.assertRaises(RuntimeError):
                request(self.url, timeout=60)
        self.assertEqual(set(threading.enumerate()), before)

    def test_system_dns_cancellation_ceiling_is_explicit(self):
        entered, release = threading.Event(), threading.Event()
        resolver = socket.getaddrinfo
        def slow_system_dns(*args, **kwargs):
            entered.set()
            release.wait(5)
            return resolver(*args, **kwargs)
        token = CancellationToken()
        with patch("w3sper.transport.socket.getaddrinfo", slow_system_dns), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.network.ping, timeout=0, cancel=token)
            try:
                self.assertTrue(entered.wait(2))
                token.cancel()
                # getaddrinfo is an OS call: no hidden/leaked resolver thread is
                # spawned. Cancellation is checked immediately after it returns.
                self.assertFalse(future.done())
            finally:
                release.set()
            with self.assertRaises(CancelledError):
                future.result(2)
        self.assertEqual(self.server.requests, [])

    def test_cancel_pending_headers_and_body_with_deadline_disabled(self):
        for mode in ("headers", "body"):
            self.server.mode = mode
            self.server.started.clear()
            self.server.release.clear()
            token = CancellationToken()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.network.query, "answer", timeout=0, cancel=token)
                try:
                    self.assertTrue(self.server.started.wait(2))
                    token.cancel("test cancellation")
                    with self.assertRaises(CancelledError):
                        future.result(2)
                finally:
                    self.server.release.set()
            self.assertEqual(token._callbacks, set())

    def test_total_deadline_covers_headers_body_and_paused_stream(self):
        for mode in ("headers", "body"):
            self.server.mode = mode
            self.server.release.clear()
            try:
                with self.assertRaises(TimeoutError):
                    self.network.query("answer", timeout=0.05)
            finally:
                self.server.release.set()
        self.server.mode = "body"
        self.server.release.clear()
        response = request(self.url, timeout=0.1)
        chunks = response.iter_chunks(1)
        self.assertEqual(next(chunks), b"{")
        time.sleep(0.2)
        with self.assertRaises(TimeoutError):
            next(chunks)
        self.assertTrue(response._closed)
        self.server.release.set()

    def test_cancel_silent_tls_handshake(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        seen = threading.Event()
        ended = threading.Event()
        def peer():
            conn, _ = listener.accept()
            with conn:
                conn.recv(8192)
                seen.set()
                conn.recv(8192)  # EOF when cancellation closes the TLS socket
            ended.set()
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        token = CancellationToken()
        with Network(f"https://127.0.0.1:{listener.getsockname()[1]}", timeout=0) as network, ThreadPoolExecutor(1) as pool:
            future = pool.submit(network.ping, cancel=token)
            try:
                self.assertTrue(seen.wait(2))
                token.cancel()
                with self.assertRaises(CancelledError):
                    future.result(2)
                self.assertTrue(ended.wait(2))
            finally:
                token.cancel()
                listener.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_tcp_poll_honors_cancellation(self):
        # Keep a real, established nonblocking local socket unready to model a
        # black-holed SYN deterministically rather than relying on host routing.
        from w3sper.transport import connect_socket
        import errno
        real_socket = socket.socket
        seen = threading.Event()
        token = CancellationToken()
        class PendingSocket(real_socket):
            def connect_ex(self, address):
                super().connect_ex(address)
                seen.set()
                return errno.EINPROGRESS
        def not_ready(*args):
            time.sleep(0.02)
            return [], [], []
        with Budget(0, token) as budget, patch("w3sper.transport.socket.socket", PendingSocket), patch("w3sper.transport.select.select", not_ready), ThreadPoolExecutor(1) as pool:
            future = pool.submit(connect_socket, "127.0.0.1", self.server.server_port, budget)
            self.assertTrue(seen.wait(2))
            token.cancel()
            with self.assertRaises(CancelledError):
                future.result(2)


class RuesTests(HTTPTestCase):
    def setUp(self):
        super().setUp()
        self.ws_ready = threading.Event()
        self.socket = None
        self.pong = True
        self.hello = True
        self.stop_peer = threading.Event()
        def peer(ws):
            self.socket = ws
            self.ws_ready.set()
            try:
                if self.hello:
                    ws.send("test-session")
                for message in ws:
                    if isinstance(message, str) and message.startswith("rues:ping:") and self.pong:
                        ws.send("rues:pong:" + message[10:])
            except ConnectionClosed:
                pass
        self.ws_server = serve(peer, "127.0.0.1", 0)
        self.ws_thread = threading.Thread(target=self.ws_server.serve_forever, daemon=True)
        self.ws_thread.start()
        self.ws_url = f"http://127.0.0.1:{self.ws_server.socket.getsockname()[1]}"
        self.network.url = self.ws_url
        http_url = self.url
        def registration(path, method, session, *, budget=None, timeout=...):
            self.assertEqual(session, "test-session")
            with request(http_url + path, method=method, headers={"rusk-session-id": session},
                         budget=budget, timeout=5 if timeout is ... else timeout) as response:
                response.read()
        self.network._subscription_request = registration
        self.addCleanup(self.cleanup_ws)

    def cleanup_ws(self):
        self.network.disconnect()
        self.ws_server.shutdown()
        self.ws_thread.join(5)

    def emit(self, topic="executed", identifier="ab", payload=None, scope="transactions"):
        data = {"gas_spent": 0, "inner": {"fee": {"gas_price": "3"}, "memo": "6869", "call": None}} if payload is None else payload
        headers = json.dumps({"Content-Location": f"/on/{scope}:{identifier}/{topic}",
                              "Content-Type": "application/octet-stream" if isinstance(data, bytes) else "application/json"}).encode()
        message = len(headers).to_bytes(4, "little") + headers + (data if isinstance(data, bytes) else json.dumps(data).encode())
        self.socket.send(message)

    def test_real_session_ping_and_reconnect(self):
        self.network.connect()
        self.assertTrue(self.network.connected)
        self.network.ping_websocket(timeout=2)
        self.pong = False
        with self.assertRaises(TimeoutError):
            self.network.ping_websocket(timeout=0.05)
        self.assertTrue(self.network.connected)
        self.pong = True
        self.network.ping_websocket(timeout=2)
        self.network.disconnect()
        self.assertFalse(self.network.connected)
        self.network.connect()
        self.network.ping_websocket(timeout=2)

    def test_registration_ack_race_refcounts_and_event_snapshots(self):
        self.network.connect()
        self.server.hook = lambda handler: self.emit() if handler.command == "GET" else None
        a = self.network.transactions.with_id("ab").subscribe("executed")
        b = self.network.transactions.with_id("ab").subscribe("executed")
        event = a.get(timeout=2)
        self.assertEqual(event.gas_paid, 0)
        self.assertEqual(event.memo(as_text=True), "hi")
        value = event.payload
        value["inner"]["memo"] = "00"
        self.assertEqual(event.memo(), b"hi")
        self.assertEqual([r[0] for r in self.server.requests], ["GET"])
        a.close()
        self.assertEqual([r[0] for r in self.server.requests], ["GET"])
        self.emit()
        self.assertEqual(b.get(timeout=2).origin.id, "ab")
        b.close()
        self.assertEqual([r[0] for r in self.server.requests], ["GET", "DELETE"])
        self.assertFalse(self.network.rues._subscriptions)

    def test_component_events_cancellation_and_overflow_cleanup(self):
        self.network.connect()
        token = CancellationToken()
        a = self.network.transactions.subscribe("executed", max_queue=1)
        b = self.network.transactions.subscribe("executed", cancel=token)
        for _ in range(2):
            self.emit()
            b.get(timeout=2)
        deadline = time.monotonic() + 2
        while not a.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        with self.assertRaisesRegex(NetworkError, "overflow"):
            a.get(timeout=2)
        self.assertEqual([r[0] for r in self.server.requests], ["GET"])
        token.cancel()
        with self.assertRaises(CancelledError):
            b.get(timeout=2)
        deadline = time.monotonic() + 2
        while not any(r[0] == "DELETE" for r in self.server.requests) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([r[0] for r in self.server.requests], ["GET", "DELETE"])
        a.close()
        b.close()

    def test_once_timeout_callback_stop_and_disconnect(self):
        self.network.connect()
        with self.assertRaises(TimeoutError):
            self.network.transactions.once("executed", timeout=0.05)
        got = threading.Event()
        subscription = self.network.transactions.on("executed", lambda _: got.set())
        self.emit()
        self.assertTrue(got.wait(2))
        worker = subscription._worker
        subscription.close()
        self.assertFalse(worker.is_alive())
        with self.network.transactions.subscribe("executed") as sub:
            self.network.disconnect()
            with self.assertRaises(CancelledError):
                sub.get(timeout=1)
        self.assertFalse(self.network.rues._subscriptions)

    def test_cancel_hello_handshake_and_disconnect_during_connect(self):
        self.hello = False
        for disconnect in (False, True):
            self.ws_ready.clear()
            token = CancellationToken()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.network.connect, timeout=0, cancel=token)
                self.assertTrue(self.ws_ready.wait(2))
                if disconnect:
                    self.network.disconnect()
                else:
                    token.cancel()
                with self.assertRaises(CancelledError):
                    future.result(2)
            self.assertFalse(self.network.connected)

    def test_cancel_silent_http_upgrade(self):
        # The HTTP server deliberately never sends a 101 response.
        self.network.url = self.url
        self.server.mode = "headers"
        token = CancellationToken()
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.network.connect, timeout=0, cancel=token)
            try:
                self.assertTrue(self.server.started.wait(2))
                token.cancel()
                with self.assertRaises(CancelledError):
                    future.result(2)
            finally:
                self.server.release.set()
        self.assertFalse(self.network.connected)

    def test_contract_events_use_real_wasm_decoder_and_cancel_lifetime(self):
        from pathlib import Path
        self.network.data_drivers.register(TRANSFER, Path(__file__).parent / "fixtures/transfer-driver.wasm")
        contract = Contract(TRANSFER, network=self.network)
        self.network.connect()
        body = bytes.fromhex(TRANSFER + STAKE) + (123).to_bytes(8, "little")
        def emit():
            self.emit("contract_to_contract", TRANSFER, body, "contracts")
        self.server.hook = lambda handler: emit() if handler.command == "GET" else None
        decoded = contract.events("contract_to_contract").once(timeout=2)
        self.assertEqual(decoded, {"sender": TRANSFER, "receiver": STAKE, "value": "123"})
        self.server.hook = None
        token, called = CancellationToken(), threading.Event()
        subscription = contract.events("contract_to_contract").on(lambda _: called.set(), cancel=token)
        emit()
        self.assertTrue(called.wait(2))
        worker = subscription._subscription._worker
        token.cancel()
        subscription.close()
        self.assertFalse(worker.is_alive())
        self.assertTrue(subscription._token.cancelled)

    def test_malformed_event_frames(self):
        for frame in (b"", b"\xff" * 4, b"\x02\0\0\0{}", "text"):
            with self.assertRaises(NetworkError):
                RuesEvent.parse(frame)


if __name__ == "__main__":
    unittest.main()
