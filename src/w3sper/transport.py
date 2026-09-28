# SPDX-License-Identifier: MPL-2.0
"""Finite, cancellable stdlib HTTP transport. No background request workers."""
import errno
import http.client
import json
import select
import socket
import ssl
from urllib.parse import urlsplit

from ._validation import octets
from .cancellation import Budget


class NetworkError(RuntimeError):
    pass


class HTTPStatusError(NetworkError):
    def __init__(self, status, body=b""):
        self.status, self.body = status, bytes(body)
        detail = self.body[:2048].decode("utf-8", "replace")
        super().__init__(f"unexpected HTTP status {status}" + (f": {detail}" if detail else ""))


class Response:
    def __init__(self, connection, response, budget, own_budget, remove_cancel, sock):
        self._connection, self._response = connection, response
        self._budget, self._own_budget = budget, own_budget
        self._remove_cancel = remove_cancel
        self.status = response.status
        self.headers = dict(response.getheaders())
        self._socket = sock
        self._closed = False
        self._remove_abort = lambda: None
        self._remove_abort = budget.token.add_callback(self.close)

    def iter_chunks(self, size=65536):
        if type(size) is not int or size < 1:
            raise ValueError("positive chunk size required")
        try:
            while True:
                self._budget.check()
                if self._closed or self._response.isclosed():
                    return
                try:
                    if self._socket:
                        self._socket.settimeout(self._budget.remaining())
                    chunk = self._response.read1(size)
                except BaseException:
                    self._budget.check()
                    if self._closed:
                        return
                    raise
                self._budget.check()
                if not chunk or self._closed:
                    return
                yield chunk
        finally:
            self.close()

    def read(self):
        return b"".join(self.iter_chunks())

    def json(self):
        try:
            return json.loads(self.read())
        except (ValueError, UnicodeError) as exc:
            raise NetworkError("invalid JSON response") from exc

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._remove_cancel()
        self._remove_abort()
        # Interrupt before closing the buffered reader: close alone can wait for
        # a concurrent read's lock, preventing the cancellation callback itself.
        if self._socket:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self._response.close()
        finally:
            self._connection.close()
            if self._own_budget:
                self._budget.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        if hasattr(self, "_closed"):
            self.close()


def _wait_socket(sock, budget, *, writing=False):
    while True:
        budget.check()
        wait = min(budget.remaining() or 0.05, 0.05)
        readable, writable, errors = select.select([] if writing else [sock], [sock] if writing else [], [sock], wait)
        budget.check()
        if readable or writable or errors:
            return


def connect_socket(host, port, budget):
    """Cancellable TCP connection. System DNS resolution itself is synchronous."""
    budget.check()
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    budget.check()
    error = OSError("no address for host")
    for family, kind, protocol, _, address in addresses:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.setblocking(False)
            code = sock.connect_ex(address)
            if code not in (0, errno.EISCONN):
                if code not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
                    raise OSError(code, "TCP connection failed")
                _wait_socket(sock, budget, writing=True)
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code:
                    raise OSError(code, "TCP connection failed")
            budget.check()
            sock.settimeout(budget.remaining())
            return sock
        except BaseException as exc:
            sock.close()
            budget.check()
            if not isinstance(exc, OSError):
                raise
            error = exc
    raise error


class BudgetSSLContext(ssl.SSLContext):
    def __new__(cls, budget):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, budget):
        self.budget = budget
        self.load_default_certs()

    def wrap_socket(self, sock, *args, **kwargs):
        kwargs["do_handshake_on_connect"] = False
        wrapped = super().wrap_socket(sock, *args, **kwargs)
        try:
            wrapped.setblocking(False)
            while True:
                self.budget.check()
                try:
                    wrapped.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    _wait_socket(wrapped, self.budget)
                except ssl.SSLWantWriteError:
                    _wait_socket(wrapped, self.budget, writing=True)
            self.budget.check()
            wrapped.settimeout(self.budget.remaining())
            return wrapped
        except BaseException:
            wrapped.close()
            raise


def request(url, *, method="POST", body=b"", headers=None, budget=None, timeout=120, cancel=None):
    # Validate before owning timed resources, including urlsplit/port failures.
    if not isinstance(url, str):
        raise TypeError("HTTP URL must be text")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ValueError("invalid HTTP URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    body = octets(body, "HTTP body")
    own_budget = budget is None
    budget = Budget(timeout, cancel) if own_budget else budget
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = None
    sockets = []
    def interrupt():
        for sock in tuple(sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
    remove = budget.token.add_callback(interrupt)
    try:
        connection = cls(parsed.hostname, port, timeout=budget.remaining())
        connection.sock = connect_socket(parsed.hostname, port, budget)
        if parsed.scheme == "https":
            connection.sock = BudgetSSLContext(budget).wrap_socket(connection.sock, server_hostname=parsed.hostname)
        sockets.append(connection.sock)
        budget.check()
        connection.sock.settimeout(budget.remaining())
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        budget.check()
        return Response(connection, response, budget, own_budget, remove, sockets[-1])
    except BaseException:
        remove()
        if connection is not None:
            connection.close()
        try:
            budget.check()
        finally:
            if own_budget:
                budget.close()
        raise
