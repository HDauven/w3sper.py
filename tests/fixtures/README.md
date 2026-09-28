# Fixed Go oracle vectors

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
