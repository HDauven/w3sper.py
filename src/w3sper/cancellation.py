# SPDX-License-Identifier: MPL-2.0
"""Cooperative cancellation and shared, wall-clock request deadlines (seconds)."""
import math
import threading
import time


class CancelledError(RuntimeError):
    pass


class CancellationToken:
    def __init__(self):
        self._lock = threading.RLock()
        self._callbacks = set()
        self._error = None
        self._event = threading.Event()

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        with self._lock:
            if self._error is not None:
                raise self._error

    def cancel(self, reason=None):
        error = reason if isinstance(reason, BaseException) else CancelledError(reason or "operation cancelled")
        with self._lock:
            if self._error is not None:
                return
            self._error = error
            self._event.set()
            callbacks, self._callbacks = tuple(self._callbacks), set()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass  # Resource cleanup must not prevent other listeners' cleanup.

    def add_callback(self, callback):
        with self._lock:
            if self._error is None:
                self._callbacks.add(callback)
                immediate = False
            else:
                immediate = True
        if immediate:
            callback()
        def remove():
            with self._lock:
                self._callbacks.discard(callback)
        return remove


def validate_timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError("timeout must be finite nonnegative seconds; zero disables the deadline")
    return value


class Budget:
    """One deadline across requests/reads; cancellation closes active resources."""
    def __init__(self, timeout=120, cancel=None):
        timeout = validate_timeout(timeout)
        if cancel is not None and not isinstance(cancel, CancellationToken):
            raise TypeError("cancel must be a CancellationToken")
        self.token = CancellationToken()
        self.deadline = time.monotonic() + timeout if timeout else None
        self._remove = cancel.add_callback(lambda: self.token.cancel(self._reason(cancel))) if cancel else lambda: None
        self._timer = None
        if timeout:
            self._timer = threading.Timer(timeout, lambda: self.token.cancel(TimeoutError("operation deadline exceeded")))
            self._timer.daemon = True
            self._timer.start()
        try:
            self.check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _reason(token):
        try:
            token.check()
        except BaseException as exc:
            return exc

    def check(self):
        self.token.check()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.token.cancel(TimeoutError("operation deadline exceeded"))
            self.token.check()

    def remaining(self):
        self.check()
        return None if self.deadline is None else max(self.deadline - time.monotonic(), 0.001)

    def close(self):
        self._remove()
        if self._timer:
            self._timer.cancel()
            if threading.current_thread() is not self._timer:
                self._timer.join()

    def __enter__(self):
        try:
            self.check()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()
