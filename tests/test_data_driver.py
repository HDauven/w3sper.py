# SPDX-License-Identifier: MPL-2.0
import json
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import wasmtime

from w3sper import (
    Bookkeeper, ClosedError, Contract, DataDriver, DataDriverError, DataDriverRegistry,
    ProfileGenerator, STAKE, TRANSFER, WalletCore, WalletCoreError,
)
from w3sper.data_driver import load

FIXTURES = Path(__file__).parent / "fixtures"
JS = json.loads((FIXTURES / "js-oracle.json").read_text())


def boundary_module(*, alloc="(param i32) (result i32) i32.const 16", dealloc="(param i32 i32)", output="i32.const 0"):
    functions = [f'(func (export "alloc") {alloc})', f'(func (export "dealloc") {dealloc})',
                 '(func (export "init"))']
    for name, params in (("get_last_error", 2), ("get_schema", 2), ("get_version", 2),
                         ("encode_input_fn", 6), ("decode_input_fn", 6), ("decode_output_fn", 6), ("decode_event", 6)):
        functions.append(f'(func (export "{name}") (param {" ".join(["i32"] * params)}) (result i32) {output})')
    return wasmtime.wat2wasm('(module (memory (export "memory") 3) ' + ' '.join(functions) + ')')


class DataDriverTests(unittest.TestCase):
    def test_actual_js_roundtrip_schema_version_and_output_decoding(self):
        for kind, vector in JS["dataDrivers"].items():
            with load(FIXTURES / (kind + "-driver.wasm")) as driver:
                self.assertEqual(driver.get_version(), vector["version"])
                self.assertEqual(driver.get_schema(), vector["schema"])
                driver.init()  # Idempotent host; never reinitializes native state.
                data = driver.encode(vector["fnName"], vector["input"])
                self.assertEqual(data.hex(), vector["encoded"])
                self.assertEqual(driver.decode_input_fn(vector["fnName"], data), vector["decoded"])
                if kind == "stake":
                    self.assertEqual(driver.decode_output_fn("get_version", (8).to_bytes(8, "little")), "8")
                else:
                    self.assertEqual(driver.decode_output_fn("chain_id", b"\x2a"), 42)
                    self.assertEqual(driver.encode("deposit", 2**64 - 1), b"\xff" * 8)
                    self.assertEqual(driver.decode_input_fn("deposit", b"\xff" * 8), str(2**64 - 1))
                    # Rust ContractToContractEvent: two ContractId[32], then u64.
                    event = bytes.fromhex(TRANSFER + STAKE) + (2**64 - 1).to_bytes(8, "little")
                    self.assertEqual(driver.decode_event("contract_to_contract", event),
                                     {"sender": TRANSFER, "receiver": STAKE, "value": str(2**64 - 1)})
                snapshot = driver.get_schema()
                snapshot["name"] = "mutated"
                self.assertNotEqual(driver.get_schema()["name"], "mutated")
            with self.assertRaises(ClosedError):
                driver.get_version()
            self.assertIsInstance(data, bytes)

    def test_real_negative_status_and_recovery(self):
        with load(FIXTURES / "stake-driver.wasm") as driver:
            for name, value in (("nonexistent", None), ("get_stake", "bad-account")):
                with self.assertRaises(DataDriverError) as caught:
                    driver.encode(name, value)
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(caught.exception.operation, "encode_input_fn")
            with self.assertRaises(DataDriverError):
                driver.decode_input_fn("get_stake", b"\0")
            with self.assertRaises(DataDriverError):
                driver.decode_event("unknown", b"")
            self.assertEqual(driver.get_version(), "0.1.0")

    def test_contract_facade_offline_and_public_authority(self):
        with WalletCore() as core, ProfileGenerator(bytes.fromhex(JS["seed"]), core) as gen:
            with gen.default_signer() as signer, load(FIXTURES / "transfer-driver.wasm") as driver:
                book = Bookkeeper(core)
                contract = Contract(TRANSFER, driver=driver, bookentry=book.bind(signer))
                self.assertEqual(contract.schema()["name"], "TransferContract")
                self.assertEqual(contract.encode("sync_accounts", [0, 5]), bytes(8) + b"\x05" + bytes(7))
                builder = contract.tx("deposit", 2).to(signer.account).chain(42).nonce(0).deposit(2)
                result = builder.build()
                self.assertEqual(result.nonce, 1)
                self.assertEqual(len(result.hash), 64)
                self.assertEqual(result.buffer[0], 1)
                with self.assertRaises(ValueError):
                    contract.call("chain_id")
                with self.assertRaises(ValueError):
                    Contract(TRANSFER, driver=driver, bookentry=book.bind(signer.profile)).tx("deposit", 2)
                for identifier in (b"", "zz" * 32, "01"):
                    with self.assertRaises(ValueError):
                        Contract(identifier, driver=driver)

    def test_registry_copy_replace_cache_concurrency_and_lifecycle(self):
        source = bytearray((FIXTURES / "stake-driver.wasm").read_bytes())
        registry = DataDriverRegistry()
        self.addCleanup(registry.close)
        registry.register(bytes.fromhex(STAKE), source)
        source[:] = bytes(len(source))
        self.assertTrue(registry.has(STAKE))
        with ThreadPoolExecutor(4) as pool:
            drivers = list(pool.map(lambda _: registry.get(STAKE), range(8)))
        self.assertTrue(all(driver is drivers[0] for driver in drivers))
        registry.register(STAKE, lambda: (FIXTURES / "stake-driver.wasm").read_bytes())
        with self.assertRaises(ClosedError):
            drivers[0].get_schema()
        self.assertEqual(registry.get(STAKE).get_version(), "0.1.0")
        with self.assertRaises(KeyError):
            registry.get(TRANSFER)
        registry.register("broken", b"bad wasm")
        for _ in range(2):
            with self.assertRaises(wasmtime.WasmtimeError):
                registry.get("broken")
        registry.close()
        with self.assertRaises(WalletCoreError):
            registry.get(STAKE)

    def test_exact_allocation_sizes_even_native_error(self):
        with load(FIXTURES / "transfer-driver.wasm") as driver:
            for bad in (False, True):
                exports = dict(driver._exports)
                allocated, freed = [], []
                def allocate(store, size):
                    pointer = exports["alloc"](store, size)
                    allocated.append((pointer, size))
                    return pointer
                def deallocate(store, pointer, size):
                    freed.append((pointer, size))
                    return exports["dealloc"](store, pointer, size)
                with patch.object(driver, "_exports", {**exports, "malloc": allocate, "free": deallocate}):
                    if bad:
                        with self.assertRaises(DataDriverError):
                            driver.encode("invalid", None)
                    else:
                        self.assertEqual(driver.encode("deposit", 2), (2).to_bytes(8, "little"))
                self.assertCountEqual(allocated, freed)

    def test_invalid_exports_output_and_bounded_cleanup(self):
        with self.assertRaisesRegex(WalletCoreError, "missing required"):
            DataDriver(wasmtime.wat2wasm("(module)"))
        with self.assertRaisesRegex(WalletCoreError, "ABI signature"):
            DataDriver(boundary_module(alloc="(param i32)"))
        with DataDriver(boundary_module(output="local.get 0 i32.const 2147483647 i32.store i32.const 0")) as driver:
            with self.assertRaisesRegex(WalletCoreError, "output size"):
                driver.get_schema()
            with self.assertRaises(ClosedError):
                driver.get_version()
        with DataDriver(boundary_module(dealloc="(param i32 i32) (loop br 0)"), fuel=1000) as driver:
            with self.assertRaises(WalletCoreError):
                driver.get_version()
            with self.assertRaises(ClosedError):
                driver.get_version()


if __name__ == "__main__":
    unittest.main()
