# Native reference fixtures

All seeds are **public synthetic test data**, never production wallet secrets.
Current compatibility is defined by the JS/native ABI at Rusk commit
`97f1cf26eeeb62d56e656871087ae2f02d960d72`, not by another language port.

## Current JS and Rust vectors

`js-oracle.json` was generated independently by the coordinator using the actual
pinned `w3sper.js` SDK and bundled wallet-core, not Python outputs. It contains
6 public profiles/raw accounts, 9 BLS signatures, 7 deterministic transactions
(including memo, contract, committed call, deployment and distinct-owner stake),
2 real data-driver metadata/roundtrip sets, and 3 nonempty genesis note sets with
ownership, balance, bookmark and selection expectations. The generator is
`checks/js-oracle.mjs` in the isolated parity lab. Tests compare full transaction
bytes and hashes, not just shapes.

`bls-message-signing.txt` is the pinned Rust fixture from
`wallet-core/tests/fixtures/bls-message-signing.txt`.

`stake-driver.wasm` and `transfer-driver.wasm` are the pinned source's release
WASM builds, used directly through Python's isolated driver runtime. `bob.wasm`
is the existing `contracts/bin/bob.wasm`: byte-sized `init`, `reset` and `value`.
`committed-target.wasm` is built from the included MPL-2.0 `.wat`, following
`rusk/vm/tests/committed_call.rs::host_backed_target`. It accepts/stores/returns
32 bytes and uses the host-backed B ABI required for committed calls. Bob uses
the legacy A ABI and is **not** a committed target.

SHA-256:

| File | Digest |
| --- | --- |
| stake-driver.wasm | `489ee7354c89eca3cbc870e3d146aba209bb02691e4864ce93dd04759f2902e3` |
| transfer-driver.wasm | `983a9794178a04165319c4d7be583f2530d1b14a0ca19d061c99f60c60264966` |
| bob.wasm | `b9927fe73c106888910da15b878cf7888040f88c1325565137e34c886877c27d` |
| committed-target.wasm | `81c11fbf30bb0a5eff376b11e1a754adb47f799afc4f98b38a1ffb1e2c4a291c` |

`phoenix-native.json` is a **native/live roundtrip**, not an independent JS
expected-value corpus. It freezes actual owned notes, openings, root, RNG,
unproven transaction/circuits, node-generated proof, and native proven bytes.
The exact reference node successfully preverified that proof; it was not
propagated. Python replay tests exercise the real native construction/proof
attachment ABI offline. Live tests separately proved, propagated and verified
state effects for every Phoenix transaction family.

## Historical Go regression vectors

These retained older fixtures protect existing behavior only; they do not
define current ABI compatibility. Tests omit their obsolete module-size and
invalid-overflow transaction assumptions.

`go-vectors.json` was emitted by `go_oracle.go`, run in a separate temporary
module with a local `replace` to the **unchanged** `w3sper.go` directory of:

- Repository: `dusk-network/rusk-private`, PR #359
- Commit: `e096a859f55a8e2bb3583bdb44ceed52ba4d7110`
- Toolchain: `go1.27.1 linux/amd64`; wazero `v1.12.0`
- Embedded Dusk wallet-core: `1.7.1`
- WASM SHA-256: `f4c3eaa1843a70aa5780e1acbd41ced9c1bdf59255872a3ddd88408093fde8d2`

The seed is PUBLIC TEST DATA: byte i is `(i*7+1) mod 256`, the same pattern as
Go's walletcore transaction/integration tests. Never fund these accounts.
Vectors contain public profiles at indices 0, 1, 255, their Base58 identifiers,
four complete signed transactions and hashes, module globals, and the actual
status code for an invalid serialized 96-byte account key. Transfer vectors
cover the standard Go example, high-level nonce 5 -> 6, every uint64 field at
its maximum plus u8 index/chain at 255, and a nil receiver/zero values.

The oracle separately compares Go's high-level builder result with its direct
Moonlight result for nonce 6. Python expected bytes/hashes were not used to
produce any fixture. Pure-layer known examples also come from the unchanged
Go unit tests. Additional negative tests intentionally reject Python booleans,
lossy float inputs and bookmark wrapping.

## Reproduce without editing the reference

Set `REFERENCE` to the frozen Go SDK directory (ending in `w3sper.go`) and
`PYTHON_PROJECT` to this repository, then in a shell:

```sh
export GOTOOLCHAIN=go1.27.1
work=$(mktemp -d /tmp/w3sper-python-oracle.XXXXXX)
cd "$work"
go mod init w3sper-python-oracle
go mod edit -go=1.25.0 -require=github.com/dusk-network/w3sper-go@v0.0.0
go mod edit -replace="github.com/dusk-network/w3sper-go=$REFERENCE"
cp "$PYTHON_PROJECT/tests/fixtures/go_oracle.go" main.go
go mod tidy
go run . "$REFERENCE/walletcore/wallet-core-1.7.1.wasm" > go-vectors.json
cmp go-vectors.json "$PYTHON_PROJECT/tests/fixtures/go-vectors.json"
```

The temporary module, not the reference, receives `go.mod`/`go.sum` changes.
The oracle is test tooling only and is not part of the Python runtime package.
