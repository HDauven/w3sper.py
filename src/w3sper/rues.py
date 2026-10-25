# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""Real RUES WebSocket sessions, ref-counted HTTP subscriptions and event streams."""
from collections import deque
from dataclasses import dataclass
import copy
import json
import re
import secrets
import threading
import time
from types import MappingProxyType
from urllib.parse import urlsplit

from websockets.sync.client import connect as websocket_connect, ClientConnection
from websockets.exceptions import ConnectionClosed

from .cancellation import Budget, CancellationToken, CancelledError
from .transport import NetworkError, connect_socket, BudgetSSLContext
from ._validation import octets


@dataclass(frozen=True)
class EventOrigin:
    scope: str
    topic: str
    id: str | None = None

    def __str__(self):
        return f"/on/{self.scope}" + (f":{self.id}" if self.id else "") + f"/{self.topic}"


class RuesEvent:
    def __init__(self, headers, payload, origin):
        self.headers = MappingProxyType(dict(headers))
        self._payload = copy.deepcopy(payload)
        self.origin = origin

    @property
    def payload(self):
        return copy.deepcopy(self._payload)

    @classmethod
    def parse(cls, message):
        if not isinstance(message, bytes) or len(message) < 4:
            raise NetworkError("invalid RUES event frame")
        size = int.from_bytes(message[:4], "little")
        if size > len(message) - 4:
            raise NetworkError("truncated RUES headers")
        try:
            raw = json.loads(message[4:4 + size])
            headers = {str(k).lower(): v for k, v in dict(raw).items()}
            location = headers["content-location"]
            match = re.fullmatch(r"/?on/([^/:]+)(?::([^/]+))?/([^/]+)", location)
            if not match:
                raise ValueError("invalid event origin")
            scope, identifier, topic = match.groups()
            body = message[4 + size:]
            if headers.get("content-type", "").split(";")[0] == "application/octet-stream":
                payload = body
            else:
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    if headers.get("content-type", "").split(";")[0] == "application/json":
                        raise
                    payload = body
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise NetworkError("malformed RUES event") from exc
        event_type = TransactionExecutedEvent if scope == "transactions" and topic == "executed" else cls
        return event_type(headers, payload, EventOrigin(scope, topic, identifier))


class TransactionExecutedEvent(RuesEvent):
    @property
    def gas_paid(self):
        return int(self._payload["gas_spent"]) * int(self._payload["inner"]["fee"]["gas_price"])

    def memo(self, *, as_text=False):
        raw = self._payload["inner"].get("memo")
        if not raw:
            return None
        result = bytes.fromhex(raw)
        return result.decode("utf-8") if as_text else result

    def call(self):
        return copy.deepcopy(self._payload["inner"].get("call"))


class Subscription:
    """Context-managed, bounded event queue. Overflow fails instead of dropping events."""
    def __init__(self, rues, target, topic, cancel, max_queue):
        if type(max_queue) is not int or max_queue < 1:
            raise ValueError("max_queue must be a positive integer")
        self._rues, self.target, self.topic = rues, target, topic
        self.path = target.path(topic)
        self._condition = threading.Condition()
        self._queue = deque()
        self._limit = max_queue
        self._closed = False
        self._error = None
        self._remove = lambda: None
        self._worker = None
        self._session = rues.session_id
        self._cancel = cancel
        self._lifetime = CancellationToken()
        if cancel:
            self._remove = cancel.add_callback(self._cancelled)

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def _cancelled(self):
        try:
            self._cancel.check()
        except BaseException as exc:
            self._finish(exc)

    def _finish(self, error=None):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._error = error
            self._queue.clear()
            self._condition.notify_all()
        self._remove()
        self._lifetime.cancel(error or CancelledError("subscription stopped"))

    def _put(self, event):
        with self._condition:
            if self._closed:
                return
            if len(self._queue) >= self._limit:
                self._finish(NetworkError("RUES subscription queue overflow"))
            else:
                self._queue.append(event)
                self._condition.notify_all()

    def get(self, *, timeout=120, cancel=None, _budget=None):
        own = _budget is None
        budget = Budget(timeout, cancel) if own else _budget
        def wake():
            with self._condition:
                self._condition.notify_all()
        remove = budget.token.add_callback(wake)
        try:
            with self._condition:
                while True:
                    budget.check()
                    if self._closed:
                        if self._error:
                            raise self._error
                        raise StopIteration
                    if self._queue:
                        return self._queue.popleft()
                    self._condition.wait(budget.remaining())
        finally:
            remove()
            if own:
                budget.close()

    def on(self, callback):
        if not callable(callback):
            raise TypeError("callback must be callable")
        if self._worker:
            raise ValueError("callback already installed")
        def run():
            try:
                for event in self:
                    if not self.closed:
                        callback(event)
            except BaseException as exc:
                self._finish(exc)
            finally:
                self._rues._unsubscribe(self)
        self._worker = threading.Thread(target=run, name="w3sper-rues-listener", daemon=True)
        self._worker.start()
        return self

    def close(self):
        self._finish()
        self._rues._unsubscribe(self)
        if self._worker and threading.current_thread() is not self._worker:
            self._worker.join()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        return self.get(timeout=0)


class Target:
    def __init__(self, network, scope, identifier=None):
        for value in (scope, identifier):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)):
                raise ValueError("invalid RUES target")
        self.network, self.scope, self.id = network, scope, identifier

    def with_id(self, identifier):
        if isinstance(identifier, (bytes, bytearray, memoryview)):
            identifier = bytes(identifier).hex()
        return Target(self.network, self.scope, identifier)

    def path(self, topic):
        if not isinstance(topic, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", topic):
            raise ValueError("invalid RUES topic")
        return f"/on/{self.scope}" + (f":{self.id}" if self.id else "") + "/" + topic

    def call(self, topic, body=b"", **options):
        return self.network.call(self.scope, topic, body, identifier=self.id, **options)

    def subscribe(self, topic, *, timeout=..., cancel=None, max_queue=1024, _budget=None):
        return self.network.rues.subscribe(self, topic, timeout=timeout, cancel=cancel,
                                            max_queue=max_queue, _budget=_budget)

    def on(self, topic, callback, **options):
        return self.subscribe(topic, **options).on(callback)

    def once(self, topic, *, timeout=..., cancel=None):
        with self.network.budget(timeout, cancel) as budget:
            with self.subscribe(topic, cancel=cancel, _budget=budget) as subscription:
                return subscription.get(_budget=budget)


class Rues:
    def __init__(self, network):
        self.network = network
        self._lock = threading.RLock()
        self._registration = threading.RLock()
        self._connect_lock = threading.Lock()
        self._socket = None
        self._reader = None
        self._session = None
        self._subscriptions = {}
        self._pings = {}
        self._handshake_cancel = None
        self._closing = False
        self._lifecycle = {"connect": set(), "disconnect": set(), "error": set()}

    def scope(self, name):
        return Target(self.network, name)

    @property
    def session_id(self):
        with self._lock:
            return self._session

    @property
    def connected(self):
        with self._lock:
            return self._socket is not None and self._session is not None and not self._closing

    def on(self, event, callback):
        with self._lock:
            self._lifecycle[event].add(callback)
        def stop():
            with self._lock:
                self._lifecycle[event].discard(callback)
        return stop

    def _emit(self, event, value=None):
        with self._lock:
            callbacks = tuple(self._lifecycle[event])
        for callback in callbacks:
            try:
                callback(value)
            except Exception:
                pass

    def connect(self, *, timeout=..., cancel=None):
        with self._connect_lock:
            if self.connected:
                return self
            with self.network.budget(timeout, cancel) as budget:
                token = CancellationToken()
                remove = budget.token.add_callback(lambda: token.cancel(Budget._reason(budget.token)))
                with self._lock:
                    self._handshake_cancel = token
                ws, sock = None, None
                stop_upgrade = []
                # Propagate disconnect-during-connect into the shared TCP/TLS budget.
                unlink = token.add_callback(lambda: budget.token.cancel(Budget._reason(token)))
                class HandshakeConnection(ClientConnection):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        stop_upgrade.append(token.add_callback(self.close_socket))
                try:
                    url = self.network.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/on"
                    parsed = urlsplit(url)
                    token.check()
                    sock = connect_socket(parsed.hostname, parsed.port or (443 if parsed.scheme == "wss" else 80), budget)
                    sock.settimeout(None)
                    tls = {"ssl": BudgetSSLContext(budget)} if parsed.scheme == "wss" else {}
                    ws = websocket_connect(url, sock=sock, open_timeout=budget.remaining(), close_timeout=2,
                                           ping_interval=None, proxy=None, max_size=16 * 1024 * 1024,
                                           create_connection=HandshakeConnection, **tls)
                    stop = token.add_callback(ws.close_socket)
                    try:
                        hello = ws.recv(timeout=budget.remaining())
                        token.check()
                        if not isinstance(hello, str) or not hello:
                            raise NetworkError("invalid RUES session handshake")
                    finally:
                        stop()
                    with self._lock:
                        token.check()
                        self._socket, self._session, self._closing = ws, hello, False
                        self._reader = threading.Thread(target=self._receive, args=(ws,),
                                                        name="w3sper-rues", daemon=True)
                        self._reader.start()
                    self._emit("connect")
                    return self
                except BaseException:
                    if ws:
                        ws.close()
                    elif sock:
                        sock.close()
                    token.check()
                    raise
                finally:
                    for stop in stop_upgrade:
                        stop()
                    unlink()
                    remove()
                    with self._lock:
                        self._handshake_cancel = None

    def _receive(self, ws):
        error = CancelledError("RUES connection closed")
        last_keepalive = time.monotonic()
        try:
            while True:
                try:
                    message = ws.recv(timeout=0.1)
                except TimeoutError:
                    message = None
                if isinstance(message, str):
                    if not message.startswith("rues:pong:"):
                        raise NetworkError("unexpected RUES text message")
                    with self._lock:
                        probe = self._pings.get(message[10:])
                    if probe:
                        probe.set()
                elif message is not None:
                    event = RuesEvent.parse(message)
                    paths = {str(event.origin), str(EventOrigin(event.origin.scope, event.origin.topic))}
                    with self._lock:
                        subscribers = [sub for path in paths for sub in self._subscriptions.get(path, ())]
                    for sub in subscribers:
                        sub._put(event)
                if time.monotonic() - last_keepalive >= 30:
                    ws.send("")
                    last_keepalive = time.monotonic()
                # Cleanup cancelled/overflowed queues without spawning worker threads.
                if self._registration.acquire(blocking=False):
                    try:
                        with self._lock:
                            retired = [s for group in self._subscriptions.values() for s in group if s.closed]
                        for sub in retired:
                            self._unsubscribe(sub)
                    finally:
                        self._registration.release()
        except ConnectionClosed:
            pass
        except BaseException as exc:
            error = exc
            self._emit("error", exc)
        finally:
            ws.close()
            with self._lock:
                if self._socket is ws:
                    self._socket, self._session = None, None
                subscriptions = [s for group in self._subscriptions.values() for s in group]
                self._subscriptions.clear()
                pings = tuple(self._pings.values())
            for sub in subscriptions:
                sub._finish(error)
            for ping in pings:
                ping.set()
            self._emit("disconnect", error)

    def disconnect(self, *, timeout=..., cancel=None):
        with self.network.budget(timeout, cancel) as budget:
            with self._lock:
                if self._handshake_cancel:
                    self._handshake_cancel.cancel("connection cancelled")
                self._closing = True
                ws, reader = self._socket, self._reader
                workers = [sub._worker for group in self._subscriptions.values() for sub in group if sub._worker]
            remove = budget.token.add_callback(ws.close_socket) if ws else lambda: None
            try:
                if ws:
                    ws.close()
                for thread in (reader, *workers):
                    if thread and thread is not threading.current_thread():
                        while thread.is_alive():
                            budget.check()
                            thread.join(min(budget.remaining() or 0.05, 0.05))
                budget.check()
            finally:
                remove()

    def ping(self, *, timeout=..., cancel=None):
        with self.network.budget(timeout, cancel) as budget:
            nonce = secrets.token_hex(16)
            event = threading.Event()
            with self._lock:
                if not self.connected:
                    raise NetworkError("no open RUES session")
                ws = self._socket
                self._pings[nonce] = event
            remove = budget.token.add_callback(event.set)
            try:
                ws.send("rues:ping:" + nonce)
                event.wait(budget.remaining())
                budget.check()
                if not self.connected or self._socket is not ws:
                    raise CancelledError("RUES connection closed during probe")
            finally:
                remove()
                with self._lock:
                    self._pings.pop(nonce, None)

    def subscribe(self, target, topic, *, timeout=..., cancel=None, max_queue=1024, _budget=None):
        own = _budget is None
        budget = self.network.budget(timeout, cancel) if own else _budget
        sub = None
        try:
            budget.check()
            with self._registration:
                with self._lock:
                    if not self.connected:
                        raise NetworkError("connect before subscribing")
                    sub = Subscription(self, target, topic, cancel, max_queue)
                    group = self._subscriptions.setdefault(sub.path, set())
                    first = not group
                    group.add(sub)  # Install before registration acknowledgement.
                if first:
                    self.network._subscription_request(sub.path, "GET", sub._session, budget=budget)
                budget.check()
                if sub.closed:
                    if sub._error:
                        raise sub._error
                    raise CancelledError("subscription closed during registration")
                return sub
        except BaseException:
            if sub:
                sub.close()
            raise
        finally:
            if own:
                budget.close()

    def _unsubscribe(self, sub):
        with self._registration:
            with self._lock:
                group = self._subscriptions.get(sub.path)
                if group is None or sub not in group:
                    return
                group.remove(sub)
                if group:
                    return
                self._subscriptions.pop(sub.path, None)
                active = self.connected and self._session == sub._session
            if active:
                try:
                    self.network._subscription_request(sub.path, "DELETE", sub._session, timeout=5)
                except Exception:
                    pass  # best-effort remote cleanup; local listener is always retired
