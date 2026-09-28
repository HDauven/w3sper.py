# SPDX-License-Identifier: MPL-2.0
"""Minimal host for Dusk wallet-core 1.7.1; cryptography stays in the WASM."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from importlib.resources import files
from threading import Lock

import wasmtime

from ._validation import octets, uint
from .values import DEFAULT_GAS_LIMIT, DEFAULT_GAS_PRICE, validate_chain_id

VERSION = "1.7.1"
WASM_RESOURCE = f"wallet-core-{VERSION}.wasm"
WASM_SHA256 = "f4c3eaa1843a70aa5780e1acbd41ced9c1bdf59255872a3ddd88408093fde8d2"


class WalletCoreError(RuntimeError):
    """Wallet-core host/runtime failure."""


class ClosedError(WalletCoreError):
    """An operation was attempted after close()."""


class CodeError(WalletCoreError):
    """Nonzero wallet-core status, preserving the operation and numeric code."""

    def __init__(self, operation: str, code: int):
        self.operation = operation
        self.code = code
        super().__init__(f"wallet-core: {operation}: error code {code}")


@dataclass(frozen=True)
class Globals:
    key_size: int
    item_size: int
    minimum_stake: int
    heap_base: int


class WalletCore:
    """One isolated WASM instance. Use as a context manager or call close().

    Calls, output copies, frees and close are serialized on this instance.
    No WASI, host imports, network access or Python cryptography are provided.
    """

    def __init__(self):
        self._lock = Lock()
        self._closed = True
        self._resources = ExitStack()
        try:
            engine = self._resources.enter_context(wasmtime.Engine())
            module = self._resources.enter_context(
                wasmtime.Module(engine, files("w3sper").joinpath(WASM_RESOURCE).read_bytes())
            )
            self._store = self._resources.enter_context(wasmtime.Store(engine))
            self._exports = wasmtime.Instance(self._store, module, []).exports(self._store)
            self._memory = self._exports["memory"]
            self.globals = Globals(
                self._global_value("KEY_SIZE", 4),
                self._global_value("ITEM_SIZE", 4),
                self._global_value("MINIMUM_STAKE", 8),
                self._exports["__heap_base"].value(self._store),
            )
            if self.globals.key_size != 32:
                raise WalletCoreError("unexpected wallet-core KEY_SIZE")
            self._closed = False
        except BaseException:
            self._resources.close()
            raise

    def __enter__(self):
        with self._lock:
            self._check_alive()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Wait for an in-flight operation and release resources; idempotent."""
        with self._lock:
            self._closed = True
            self._resources.close()

    def _check_alive(self):
        if self._closed:
            raise ClosedError("wallet-core: instance is closed")

    @contextmanager
    def _operation(self):
        # ponytail: one lock per instance; use separate instances for parallel work.
        with self._lock:
            self._check_alive()
            try:
                with ExitStack() as allocations:
                    yield allocations
            except CodeError:
                raise  # A reported input/protocol error does not poison the instance.
            except WalletCoreError:
                # An invalid ABI pointer/length cannot safely be freed by guessing.
                self._closed = True
                self._resources.close()
                raise
            except (wasmtime.Trap, wasmtime.WasmtimeError) as exc:
                self._closed = True
                self._resources.close()
                raise WalletCoreError("wallet-core execution failed; instance closed") from exc

    def _range(self, pointer: int, size: int):
        if pointer < 0 or size < 0 or pointer + size > self._memory.data_len(self._store):
            raise WalletCoreError("wallet-core memory access out of bounds")

    def _read(self, pointer: int, size: int) -> bytes:
        self._range(pointer, size)
        # Wasmtime.read copies; bytes makes the result immutable as well.
        return bytes(self._memory.read(self._store, pointer, pointer + size))

    def _global_value(self, name: str, size: int) -> int:
        # These exported globals are addresses of static values, not values.
        pointer = self._exports[name].value(self._store) & 0xFFFFFFFF
        return int.from_bytes(self._read(pointer, size), "little")

    def _free(self, pointer: int, size: int, secret: bool = False):
        if secret:
            self._memory.write(self._store, b"\0" * size, pointer)
        self._exports["free"](self._store, pointer, size)

    def _box(self, allocations: ExitStack, data: bytes, *, secret: bool = False) -> int:
        size = len(data)
        if not 0 < size <= 0xFFFFFFFF:
            raise WalletCoreError("invalid wallet-core allocation size")
        pointer = self._exports["malloc"](self._store, size) & 0xFFFFFFFF
        if not pointer:
            raise WalletCoreError("wallet-core allocation failed")
        self._range(pointer, size)
        allocations.callback(self._free, pointer, size, secret)
        self._memory.write(self._store, data, pointer)
        return pointer

    def _invoke(self, operation: str, *args):
        code = self._exports[operation](self._store, *args)
        if not isinstance(code, int):
            raise WalletCoreError(f"wallet-core: {operation}: missing status")
        if code:
            raise CodeError(operation, code)

    def _take_output(self, allocations: ExitStack, slot: int) -> bytes:
        pointer = int.from_bytes(self._read(slot, 4), "little")
        if not pointer:
            raise WalletCoreError("wallet-core returned a null output")
        size = int.from_bytes(self._read(pointer, 4), "little")
        self._range(pointer, 4 + size)
        # Output ABI: slot -> [u32 little-endian payload length][payload].
        # free MUST receive the allocation size, including the length prefix.
        allocations.callback(self._free, pointer, 4 + size)
        return self._read(pointer + 4, size)

    def generate_profile(self, seed: bytes, index: int = 0) -> bytes:
        """Return owned 160-byte public data (64-byte address || 96-byte account)."""
        with self._operation() as allocations:
            seed = octets(seed, "seed", 64)
            uint(index, 8, "profile index")
            seed_ptr = self._box(allocations, seed, secret=True)
            output = self._box(allocations, bytes(160))
            self._invoke("generate_profile", seed_ptr, index, output)
            return self._read(output, 160)

    def moonlight(
        self, seed: bytes, sender_index: int, receiver: bytes | None, value: int,
        *, nonce: int, chain_id: int, deposit: int = 0,
        gas_limit: int = DEFAULT_GAS_LIMIT, gas_price: int = DEFAULT_GAS_PRICE,
    ) -> tuple[bytes, str]:
        """Sign a value transfer. nonce is the FINAL nonce, not the previous one.

        None receiver means self, per wallet-core. No contract data is accepted.
        Returns owned network transaction bytes (without ABI prefix) and hash.
        """
        with self._operation() as allocations:
            seed = octets(seed, "seed", 64)
            uint(sender_index, 8, "sender index")
            if receiver is not None:
                receiver = octets(receiver, "account key", 96)
            validate_chain_id(chain_id)
            values = (("value", value), ("deposit", deposit), ("gas limit", gas_limit),
                      ("gas price", gas_price), ("nonce", nonce))
            for name, number in values:
                uint(number, 64, name)
            seed_ptr = self._box(allocations, seed, secret=True)
            receiver_ptr = 0 if receiver is None else self._box(allocations, receiver)
            numbers = [self._box(allocations, number.to_bytes(8, "little")) for _, number in values]
            tx_slot = self._box(allocations, bytes(4))
            hash_out = self._box(allocations, bytes(64))
            self._invoke("moonlight", seed_ptr, sender_index, receiver_ptr, *numbers,
                         chain_id, 0, tx_slot, hash_out)
            buffer = self._take_output(allocations, tx_slot)
            hash_bytes = self._read(hash_out, 64)
            if len(hash_bytes) != 64 or any(c not in b"0123456789abcdef" for c in hash_bytes):
                raise WalletCoreError("wallet-core returned an invalid hash")
            return buffer, hash_bytes.decode("ascii")
