# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""Dusk wallet-core host. All cryptography and transaction encoding stay in WASM."""

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from importlib.resources import files
from threading import Lock
import secrets
import struct

from . import _rkyv
from .payload import ContractCall, DeployData, Memo

import wasmtime

from ._validation import octets, uint
from .values import DEFAULT_GAS_LIMIT, DEFAULT_GAS_PRICE, Gas, validate_chain_id

VERSION = "1.7.1"
WASM_RESOURCE = f"wallet-core-{VERSION}.wasm"
WASM_SHA256 = "f17a0b4a77044e4424a7a400d73d64d3366e41cd0e417d1cdd1d4b38b48185fc"


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
    heap_base: int | None = None


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
                # Current wallet-core owns its allocator; __heap_base is not exported.
                None,
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
        # One lock per instance; use separate instances for parallel work.
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
        if size == 0:
            data, size = b"\0", 1  # valid non-null pointer for an empty Rust slice
        if not 0 < size <= 0x7FFFFFFF:
            raise ValueError("invalid wallet-core allocation size")
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
        data: bytes | None = None,
    ) -> tuple[bytes, str]:
        """Compatibility seed-taking transfer. nonce is the FINAL nonce.

        None receiver means self. data is a raw archive from serialize_payload().
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
            Gas(gas_limit, gas_price)
            data = None if data is None else octets(data, "transaction data")
            seed_ptr = self._box(allocations, seed, secret=True)
            receiver_ptr = 0 if receiver is None else self._box(allocations, receiver)
            numbers = [self._box(allocations, number.to_bytes(8, "little")) for _, number in values]
            tx_slot = self._box(allocations, bytes(4))
            hash_out = self._box(allocations, bytes(64))
            self._invoke("moonlight", seed_ptr, sender_index, receiver_ptr, *numbers,
                         chain_id, 0 if data is None else self._box(allocations, _rkyv.prefixed(data)),
                         tx_slot, hash_out)
            buffer = self._take_output(allocations, tx_slot)
            hash_bytes = self._read(hash_out, 64)
            if len(hash_bytes) != 64 or any(c not in b"0123456789abcdef" for c in hash_bytes):
                raise WalletCoreError("wallet-core returned an invalid hash")
            return buffer, hash_bytes.decode("ascii")

    def _run(self, operation, args, outputs, *, secret_args=()):
        """Typed ABI: integers are immediate, bytes are allocated, None is NULL.

        Output descriptors: integer = fixed-size buffer, 'buffer' = allocated
        length-prefixed output. Even partial outputs on a nonzero status are freed.
        """
        with self._operation() as allocations:
            pointers = [arg if isinstance(arg, int) else 0 if arg is None else
                        self._box(allocations, arg, secret=i in secret_args)
                        for i, arg in enumerate(args)]
            slots = [self._box(allocations, bytes(4 if kind == "buffer" else kind))
                     for kind in outputs]
            try:
                self._invoke(operation, *pointers, *slots)
            except CodeError:
                for slot, kind in zip(slots, outputs):
                    if kind == "buffer" and int.from_bytes(self._read(slot, 4), "little"):
                        self._take_output(allocations, slot)
                raise
            return tuple(self._take_output(allocations, slot) if kind == "buffer"
                         else self._read(slot, kind) for slot, kind in zip(slots, outputs))

    @staticmethod
    def _number(value, name="value"):
        return uint(value, 64, name).to_bytes(8, "little")

    @staticmethod
    def _hash_result(result):
        buffer, digest = result
        if len(digest) != 64 or any(c not in b"0123456789abcdef" for c in digest):
            raise WalletCoreError("wallet-core returned an invalid hash")
        return buffer, digest.decode("ascii")

    @staticmethod
    def _rng(rng):
        return secrets.token_bytes(32) if rng is None else octets(rng, "randomness", 32)

    def generate_profile_keys(self, seed, index=0):
        """Internal secret-bearing output, owned and mutable; caller must wipe it."""
        seed = octets(seed, "seed", 64)
        uint(index, 8, "profile index")
        with self._operation() as allocations:
            ptr = self._box(allocations, seed, secret=True)
            output = self._box(allocations, bytes(320), secret=True)
            self._invoke("generate_profile_keys", ptr, index, output)
            return bytearray(self._read(output, 320))

    def sign_bls_message_with_key(self, key, message):
        key = octets(key, "BLS secret key", 32)
        message = octets(message, "message")
        if len(message) > 0x7FFFFFFF - 176:
            raise ValueError("message exceeds allocation limit")
        result, = self._run("sign_bls_message_with_key",
                            [key, message or None, len(message)], [144], secret_args=(0, 1))
        return result[:96], result[96:]

    def accounts_into_raw(self, accounts):
        accounts = tuple(octets(account, "account", 96) for account in accounts)
        if not accounts:
            return ()
        result, = self._run("accounts_into_raw", [_rkyv.prefixed(b"".join(accounts))], ["buffer"])
        if len(result) % len(accounts):
            raise WalletCoreError("invalid account archive length")
        size = len(result) // len(accounts)
        return tuple(result[i:i + size] for i in range(0, len(result), size))

    def display_scalar(self, scalar):
        result, = self._run("display_scalar", [octets(scalar, "scalar", 32)], [64])
        return result.decode("ascii")

    def _notes(self, notes, *, entries=False):
        size = self.globals.item_size
        if entries:
            if not isinstance(notes, Mapping):
                raise TypeError("notes must map nullifiers to note leaves")
            return _rkyv.vector((octets(k, "nullifier", 32) + octets(v, "note", size)
                                 for k, v in notes.items()), size + 32)
        values = notes.values() if hasattr(notes, "values") else notes
        return _rkyv.vector(values, size)

    def balance_with_key(self, view_key, notes):
        key = octets(view_key, "view key", 64)
        result, = self._run("balance_with_key", [key, _rkyv.prefixed(self._notes(notes))],
                            [16], secret_args=(0,))
        return struct.unpack("<QQ", result)

    def pick_notes_with_key(self, view_key, notes, value):
        key = octets(view_key, "view key", 64)
        value = self._number(value)
        source = _rkyv.prefixed(self._notes(notes, entries=True))
        with self._operation() as allocations:
            kp = self._box(allocations, key, secret=True)
            vp = self._box(allocations, value)
            ptr = self._box(allocations, source)
            self._invoke("pick_notes_with_key", kp, vp, ptr)
            size = int.from_bytes(self._read(ptr, 4), "little")
            if size > len(source) - 4:
                raise WalletCoreError("note selection grew its input allocation")
            result = self._read(ptr + 4, size)
            return {item[:32]: item[32:] for item in
                    _rkyv.vector_items(result, self.globals.item_size + 32)}

    def map_owned_with_keys(self, keys, notes):
        keys = octets(keys, "Phoenix keys")
        if not keys or len(keys) % 64 or len(keys) > 256 * 64:
            raise ValueError("expected 1..256 Phoenix keys")
        notes = octets(notes, "note leaves")
        size = self.globals.item_size
        if len(notes) % size:
            raise ValueError("truncated note leaf")
        archive = _rkyv.vector((notes[i:i + size] for i in range(0, len(notes), size)), size)
        result, info = self._run("map_owned_with_keys", [keys, len(keys), _rkyv.prefixed(archive)],
                                 ["buffer", 16], secret_args=(0,))
        # Vec<Vec<(nullifier, NoteLeaf)>>: resolve each inner relative pointer.
        root = len(result) - 8
        offset, count = struct.unpack_from("<iI", result, root)
        if count != len(keys) // 64:
            raise WalletCoreError("unexpected ownership result count")
        _rkyv.vector_items(result, 8)  # bounds-check the outer vector
        owned = []
        for i in range(count):
            items = _rkyv.vector_items(result, size + 32, root + offset + i * 8)
            owned.append({item[:32]: item[32:] for item in items})
        height, bookmark = struct.unpack("<QQ", info)
        return tuple(owned), {"block_height": height, "bookmark": bookmark}

    def bookmarks(self, notes):
        archive = self._notes(notes)
        count = int.from_bytes(archive[-4:], "little")
        if not count:
            return ()
        with self._operation() as allocations:
            ptr = self._box(allocations, _rkyv.prefixed(archive))
            slot = self._box(allocations, bytes(4))
            self._invoke("bookmarks", ptr, slot)
            output = int.from_bytes(self._read(slot, 4), "little")
            self._range(output, count * 8)
            if not output:
                raise WalletCoreError("null bookmarks output")
            allocations.callback(self._free, output, count * 8)
            return struct.unpack("<" + "Q" * count, self._read(output, count * 8))

    def serialize_payload(self, payload):
        """Return a raw RKYV TransactionData archive (no host length prefix)."""
        def pair(data):
            return [len(data).to_bytes(4, "little"), data]
        if isinstance(payload, Memo):
            args = [None, None, None, None, bytes(32), *pair(payload.data)]
            operation = "create_tx_data"
        elif isinstance(payload, ContractCall):
            args = [*pair(payload.fn_name.encode("utf-8")), *pair(payload.fn_args), payload.contract_id]
            operation = "create_committed_call_tx_data" if payload.committed else "create_tx_data"
            if not payload.committed:
                args.extend((None, None))
        elif isinstance(payload, DeployData):
            args = [*pair(payload.bytecode), *pair(payload.owner),
                    *([None, None] if payload.init_args is None else pair(payload.init_args)),
                    self._number(payload.nonce, "deployment nonce")]
            operation = "create_deploy_tx_data"
        else:
            raise TypeError("payload must be Memo, ContractCall or DeployData")
        result, = self._run(operation, args, ["buffer"])
        return result

    def contract_id(self, bytecode, owner, nonce=0):
        payload = DeployData(bytecode, owner, nonce=nonce)
        args = [len(payload.bytecode).to_bytes(4, "little"), payload.bytecode,
                len(payload.owner).to_bytes(4, "little"), payload.owner, self._number(nonce)]
        result, = self._run("contract_id", args, [32])
        return result.hex()

    def moonlight_with_key(self, key, receiver, value, *, nonce, chain_id, deposit=0,
                           gas_limit=DEFAULT_GAS_LIMIT, gas_price=DEFAULT_GAS_PRICE, data=None):
        key = octets(key, "BLS secret key", 32)
        receiver = None if receiver is None else octets(receiver, "account", 96)
        Gas(gas_limit, gas_price)
        args = [key, receiver, self._number(value), self._number(deposit, "deposit"),
                self._number(gas_limit), self._number(gas_price), self._number(nonce, "nonce"),
                validate_chain_id(chain_id), None if data is None else _rkyv.prefixed(data)]
        return self._hash_result(self._run("moonlight_with_key", args, ["buffer", 64], secret_args=(0,)))

    def _phoenix_inputs(self, inputs, openings, root, *, entries=False):
        archive = self._notes(inputs, entries=entries)
        openings = tuple(octets(opening, "opening") for opening in openings)
        if len(openings) != int.from_bytes(archive[-4:], "little"):
            raise ValueError("one opening is required per note")
        if not 1 <= len(openings) <= 4:
            raise ValueError("Phoenix transactions require 1..4 inputs")
        if any(not op or op[0] != 1 for op in openings):
            raise ValueError("missing note opening")
        if len({len(op) for op in openings}) != 1:
            raise ValueError("inconsistent opening sizes")
        return [_rkyv.prefixed(archive), _rkyv.prefixed(_rkyv.vector(openings)),
                octets(root, "root", 32)]

    def phoenix_with_key(self, key, receiver, inputs, openings, root, value, *, chain_id,
                         deposit=0, gas_limit=DEFAULT_GAS_LIMIT, gas_price=DEFAULT_GAS_PRICE,
                         data=None, rng=None):
        key = octets(key, "Phoenix secret key", 64)
        receiver = octets(receiver, "address", 64)
        Gas(gas_limit, gas_price)
        args = [self._rng(rng), key, receiver, *self._phoenix_inputs(inputs, openings, root),
                self._number(value), self._number(deposit), self._number(gas_limit),
                self._number(gas_price), validate_chain_id(chain_id),
                None if data is None else _rkyv.prefixed(data)]
        return self._run("phoenix_with_key", args, ["buffer", "buffer"], secret_args=(1,))

    def into_proven(self, transaction, proof):
        return self._hash_result(self._run("into_proven",
                    [_rkyv.prefixed(transaction), _rkyv.prefixed(proof)], ["buffer", 64]))

    def shield(self, keys, value, *, nonce, chain_id, gas_limit=DEFAULT_GAS_LIMIT,
               gas_price=DEFAULT_GAS_PRICE, rng=None):
        keys = octets(keys, "BLS and Phoenix secret keys", 96)
        Gas(gas_limit, gas_price)
        args = [self._rng(rng), keys, self._number(value), self._number(gas_limit),
                self._number(gas_price), self._number(nonce), validate_chain_id(chain_id)]
        return self._hash_result(self._run("moonlight_to_phoenix_with_keys", args,
                                          ["buffer", 64], secret_args=(1,)))

    def unshield(self, keys, inputs, openings, root, value, *, chain_id,
                 gas_limit=DEFAULT_GAS_LIMIT, gas_price=DEFAULT_GAS_PRICE, rng=None):
        keys = octets(keys, "BLS and Phoenix secret keys", 96)
        Gas(gas_limit, gas_price)
        if not isinstance(inputs, Mapping):
            raise TypeError("unshield inputs must map nullifiers to note leaves")
        notes, ops, root = self._phoenix_inputs(inputs, openings, root)
        nullifiers = _rkyv.prefixed(_rkyv.vector(inputs.keys(), 32))
        args = [self._rng(rng), keys, notes, ops, nullifiers, root, self._number(value),
                self._number(gas_limit), self._number(gas_price), validate_chain_id(chain_id)]
        return self._run("phoenix_to_moonlight_with_keys", args, ["buffer", "buffer"], secret_args=(1,))

    def stake_transaction(self, action, keys, value, *, chain_id, nonce=None,
                          inputs=None, openings=None, root=None,
                          gas_limit=DEFAULT_GAS_LIMIT, gas_price=DEFAULT_GAS_PRICE, rng=None):
        """Build stake/unstake/reward with distinct owner keys; notes select Phoenix."""
        if action not in ("stake", "unstake", "stake_reward"):
            raise ValueError("invalid staking action")
        shielded = inputs is not None
        keys = octets(keys, "staking keys", 128 if shielded else 64)
        Gas(gas_limit, gas_price)
        randomized = shielded or action != "stake"
        args = [self._rng(rng), keys] if randomized else [keys]
        if shielded:
            args.extend(self._phoenix_inputs(inputs, openings, root, entries=action != "stake"))
        args.extend((self._number(value), self._number(gas_limit), self._number(gas_price)))
        if not shielded:
            args.append(self._number(nonce, "nonce"))
        args.append(validate_chain_id(chain_id))
        operation = ("phoenix_" if shielded else "moonlight_") + action + "_with_keys"
        result = self._run(operation, args, ["buffer", "buffer" if shielded else 64],
                           secret_args=(1 if randomized else 0,))
        return result if shielded else self._hash_result(result)
