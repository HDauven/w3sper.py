# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""WASM contract data drivers: JSON/RKYV, schema, events and cached locators."""
from contextlib import ExitStack
import json
from pathlib import Path
from threading import RLock

import wasmtime

from ._validation import octets
from .cancellation import Budget
from .payload import contract_bytes
from .transport import HTTPStatusError, request
from .walletcore import WalletCore, WalletCoreError, CodeError


class DataDriverError(CodeError):
    def __init__(self, operation, code, detail):
        self.operation, self.code = operation, code
        WalletCoreError.__init__(self, f"data-driver {operation} ({code}): {detail}")


class DataDriver(WalletCore):
    """One serialized, isolated Wasmtime instance; init runs exactly once.

    The driver controls JSON representations (u64 values are commonly strings).
    Outputs are copied; nonzero statuses preserve the native error. Runtime traps
    and malformed output close the instance. No WASI/host imports are granted.
    """
    def __init__(self, binary, *, max_output=16 * 1024 * 1024, fuel=100_000_000):
        binary = octets(binary, "data-driver WASM")
        if type(max_output) is not int or max_output < 65536:
            raise ValueError("max_output must be at least 65536")
        if type(fuel) is not int or fuel <= 0:
            raise ValueError("fuel must be positive")
        self._max_output, self._fuel = max_output, fuel
        self._lock = RLock()
        self._closed = True
        self._initialized = False
        self._resources = ExitStack()
        try:
            config = wasmtime.Config()
            config.consume_fuel = True
            engine = self._resources.enter_context(wasmtime.Engine(config))
            module = self._resources.enter_context(wasmtime.Module(engine, binary))
            self._store = self._resources.enter_context(wasmtime.Store(engine))
            self._store.set_limits(memory_size=256 * 1024 * 1024)
            self._store.set_fuel(self._fuel)
            self._exports = dict(wasmtime.Instance(self._store, module, []).exports(self._store))
            required = {"memory", "alloc", "dealloc", "get_last_error", "init", "encode_input_fn",
                        "decode_input_fn", "decode_output_fn", "decode_event", "get_schema", "get_version"}
            missing = required - self._exports.keys()
            if missing:
                raise WalletCoreError("missing required data-driver exports: " + ", ".join(sorted(missing)))
            self._memory = self._exports["memory"]
            if not isinstance(self._memory, wasmtime.Memory):
                raise WalletCoreError("invalid data-driver memory export")
            signatures = {"alloc": (1, 1), "dealloc": (2, 0), "init": (0, 0),
                          "get_last_error": (2, 1), "get_schema": (2, 1), "get_version": (2, 1),
                          "encode_input_fn": (6, 1), "decode_input_fn": (6, 1),
                          "decode_output_fn": (6, 1), "decode_event": (6, 1)}
            for name, (params, results) in signatures.items():
                function = self._exports[name]
                if not isinstance(function, wasmtime.Func):
                    raise WalletCoreError(f"invalid data-driver function export: {name}")
                signature = function.type(self._store)
                if (list(map(str, signature.params)) != ["i32"] * params or
                        list(map(str, signature.results)) != ["i32"] * results):
                    raise WalletCoreError(f"invalid data-driver ABI signature: {name}")
            self._exports["malloc"], self._exports["free"] = self._exports["alloc"], self._exports["dealloc"]
            self._closed = False
            self.init()
        except BaseException:
            self._resources.close()
            self._closed = True
            raise

    def init(self):
        with self._operation():
            if not self._initialized:
                self._store.set_fuel(self._fuel)
                self._exports["init"](self._store)
                self._initialized = True

    def _output(self, operation, args):
        size = 65536
        while True:
            # Retry serialization only on the ABI's documented BufferTooSmall.
            with ExitStack() as allocations:
                ptr = self._box(allocations, bytes(size))
                code = self._exports[operation](self._store, *args, ptr, size)
                if code == 4 and size < self._max_output and operation != "get_last_error":
                    size = min(size * 2, self._max_output)
                    continue
                if code:
                    if operation == "get_last_error":
                        raise DataDriverError(operation, code, "unable to retrieve native error")
                    detail = self._output("get_last_error", []).decode("utf-8", "replace")
                    raise DataDriverError(operation, code, detail)
                length = int.from_bytes(self._read(ptr, 4), "little")
                if length > size - 4:
                    raise WalletCoreError("invalid data-driver output size")
                return self._read(ptr + 4, length)

    def _call_driver(self, operation, name=None, data=None):
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("a nonempty ABI name is required")
        if data is not None:
            data = octets(data, "driver input")
        with self._operation() as allocations:
            self._store.set_fuel(self._fuel)
            args = []
            if name is not None:
                for item in (name.encode("utf-8"), data):
                    args.extend((self._box(allocations, item), len(item)))
            return self._output(operation, args)

    def encode_input_fn(self, name, json_text):
        if not isinstance(json_text, str):
            raise TypeError("json_text must be text; use encode() for Python values")
        return self._call_driver("encode_input_fn", name, json_text.encode("utf-8"))

    def encode(self, name, value=None):
        return self.encode_input_fn(name, json.dumps(value, allow_nan=False, separators=(",", ":")))

    def decode_input_fn(self, name, data):
        return json.loads(self._call_driver("decode_input_fn", name, data))

    def decode_output_fn(self, name, data):
        return json.loads(self._call_driver("decode_output_fn", name, data))

    def decode_event(self, name, data):
        return json.loads(self._call_driver("decode_event", name, data))

    def get_schema(self):
        return json.loads(self._call_driver("get_schema"))

    def get_version(self):
        return self._call_driver("get_version").decode("utf-8")


def load(source, *, timeout=120, cancel=None, _budget=None):
    own = _budget is None
    budget = Budget(timeout, cancel) if own else _budget
    try:
        budget.check()
        if callable(source):
            source = source()
            budget.check()
        if isinstance(source, (str, Path)):
            if str(source).startswith(("http://", "https://")):
                with request(str(source), method="GET", budget=budget) as response:
                    binary = response.read()
                    if not 200 <= response.status < 300:
                        raise HTTPStatusError(response.status, binary)
            else:
                binary = Path(source).read_bytes()
        else:
            binary = octets(source, "data-driver source")
        budget.check()
        driver = DataDriver(binary)
        try:
            budget.check()
        except BaseException:
            driver.close()
            raise
        return driver
    finally:
        if own:
            budget.close()


class DataDriverRegistry:
    def __init__(self, *, timeout=120):
        self.timeout = timeout
        self._lock = RLock()
        self._sources, self._instances = {}, {}
        self._closed = False

    @staticmethod
    def _key(key):
        # String aliases remain useful for contracts discovered by applications.
        return key.lower() if isinstance(key, str) else contract_bytes(key).hex()

    def register(self, key, locator):
        key = self._key(key)
        if isinstance(locator, (bytes, bytearray, memoryview)):
            locator = bytes(locator)
        with self._lock:
            if self._closed:
                raise WalletCoreError("registry is closed")
            old = self._instances.pop(key, None)
            self._sources[key] = locator
            if old:
                old.close()
        return self

    def has(self, key):
        with self._lock:
            return self._key(key) in self._sources

    def get(self, key, source=None, *, timeout=..., cancel=None, _budget=None):
        key = self._key(key)
        own = _budget is None
        budget = Budget(self.timeout if timeout is ... else timeout, cancel) if own else _budget
        acquired = False
        try:
            if source is not None:
                self.register(key, source)
            # Short lock waits honor cancellation while another driver is loading.
            while not acquired:
                budget.check()
                acquired = self._lock.acquire(timeout=0.05)
            if self._closed:
                raise WalletCoreError("registry is closed")
            if key not in self._instances:
                if key not in self._sources:
                    raise KeyError(f"no data-driver registered for {key}")
                self._instances[key] = load(self._sources[key], _budget=budget)
            budget.check()
            return self._instances[key]
        finally:
            if acquired:
                self._lock.release()
            if own:
                budget.close()

    def close(self):
        with self._lock:
            self._closed = True
            for driver in self._instances.values():
                driver.close()
            self._instances.clear()
            self._sources.clear()
