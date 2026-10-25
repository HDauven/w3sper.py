# Third-party notices

## Dusk wallet-core, reference SDK, and test contracts

Copyright (c) DUSK NETWORK. All rights reserved.

Dusk wallet-core, w3sper.js, contract data drivers and test contracts are licensed
under the [Mozilla Public License 2.0](LICENSE). Python implementations adapted
from their interfaces and serialization layouts retain MPL-2.0 notices.

- Source revision: `97f1cf26eeeb62d56e656871087ae2f02d960d72`
- Source: https://github.com/dusk-network/rusk-private/tree/97f1cf26eeeb62d56e656871087ae2f02d960d72
- Bundled artifact: `src/w3sper/wallet-core-1.7.1.wasm`, identical to the reference
  `w3sper.js/src/protocol-driver/wallet-core-1.7.1.wasm`
- SHA-256: `f17a0b4a77044e4424a7a400d73d64d3366e41cd0e417d1cdd1d4b38b48185fc`

Source directories: `wallet-core/`, `w3sper.js/`, `core/`, `data-drivers/`,
`contracts/tests/bob/`, and `vm/tests/committed_call.rs`.
Fixture provenance and hashes are in [tests/fixtures/README.md](tests/fixtures/README.md).
Older retained deterministic regression vectors identify their original source
revision there; they are not substitutes for current reference vectors.

## Runtime dependencies

Wasmtime is a Bytecode Alliance dependency distributed separately under
Apache-2.0 with LLVM exceptions. `websockets` is distributed separately under
the BSD 3-Clause license. Their distributions carry their own notices and
licenses; they are not embedded in wallet-core.
