# w3sper.py

A Python SDK for Dusk, importable as `w3sper`, with profile derivation,
Moonlight transaction signing, unit conversions, and a GraphQL client.

Key derivation and signing use the bundled `wallet-core-1.7.1.wasm` module
through [Wasmtime](https://github.com/bytecodealliance/wasmtime-py), the only
runtime dependency.

## Install

Requires Python 3.11+ on a platform supported by Wasmtime.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'w3sper @ git+ssh://git@github.com/HDauven/w3sper.py.git@main'
```

For a local checkout, replace the last command with `python -m pip install .`.
The WASM module is included in the package; signing needs no node connection.

## Offline transfer

```python
from w3sper import Bookkeeper, Generator, WalletCore, parse_from_dusk

# PUBLIC TEST SEED. Never put funds in accounts derived from this seed.
seed = bytes((i * 7 + 1) & 255 for i in range(64))
with WalletCore() as core:
    profiles = Generator(seed, core)
    sender = profiles.default()                 # SigningProfile at index 0
    receiver = profiles.next().profile          # public Profile at index 1
    result = (
        Bookkeeper(core).bind(sender).transfer()
        .amount(parse_from_dusk("1"))           # integer Lux, 9 decimals per Dusk
        .chain(42)
        .nonce(0)                              # CURRENT nonce; signs with 1
        .to(receiver.account_id)               # Base58, 96-byte Moonlight key
        .build()
    )
print(result.hash)  # 072cd70a7819c53f25028f5cb4f937b427823b5474d15871c4e309448fc5d133
```

Run the complete example with `python examples/offline.py` after installation.
It signs a real 293-byte transaction but does not submit it. `result.buffer`
is the network transaction, **without** the WASM ABI length prefix; the hash
is wallet-core's network-format lowercase hexadecimal string.

### Public data is not signing authority

- `Profile(raw_160_bytes)` contains only public address (64 bytes) and account
  (96 bytes). `.address`, `.account` and `bytes(profile)` return immutable bytes;
  `.address_id` and `.account_id` return Base58 strings. Parsing verifies lengths,
  not curve points; wallet-core validates receiver points during signing.
- `SigningProfile(core, seed, index)` derives public keys and retains a copied
  64-byte seed. Its `.profile` is public; `.index` is bounded to 0..255.
  `Bookkeeper.bind(public_profile)` allows balance lookup, **never signing**.
- `Generator(seed, core)` caches signing profiles. `default()` is always index 0;
  `next()` starts at 1 even if called first, and fails after 255. `derive(index)`
  is idempotent; `at(index)` only looks in the cache. `index_of()` uses generator
  identity, not public-key equality.
- Seeds are omitted from repr and the host's WASM seed buffer is wiped before
  freeing. Python/native runtime memory is **not guaranteed to be securely
  erased**. Do not log, pickle or `dataclasses.asdict()` seed-bearing objects.
  Closing `WalletCore` does not erase seeds retained by a generator/signer.

### Values, gas and bookkeeping

`format_to_dusk(int)` / `parse_from_dusk(str)` are exact; no float, exponent,
negative value or rounding. `"1."` is accepted. Conversions use arbitrary
Python integers; signing enforces unsigned 64-bit values. Boolean values are
not integers at API boundaries. Chain IDs and derivation indices are u8.

`Gas(limit=None, price=None)` defaults to limit `2_900_000_000`, price `1`;
None/non-positive arguments select those defaults. `.total` is exact.
`Bookmark(data)` wraps eight bytes; `Bookmark.from_int(n)` is strictly u64,
`int(bookmark)` reads little-endian, and `.is_none` recognizes all-ff.
Negative and overflowing bookmark integers are rejected.
`base16_*` and `base58_*` provide strict encodings (Bitcoin alphabet, no checksum).

Builders are immutable: keep the return value of `.amount()`, `.deposit()`,
`.gas(Gas(...))`, `.chain()`, `.nonce()` and `.to()`. Routes cannot alter each other.
Chain ID is required explicitly. `nonce(n)` means the **current** account nonce;
`build()` signs with `n + 1` and rejects overflow. By contrast,
`WalletCore.moonlight(..., nonce=n, chain_id=...)` takes the **final** nonce.

For caller-managed state, pass `Bookkeeper(core, account_balance=lookup)` where
`lookup(raw_96_byte_account)` returns `AccountBalance(nonce=..., value=...)`.
Use `.balance(account_id)` or `.bind(profile).balance()`; builders without an
explicit nonce use that callback. There is no balance sufficiency check,
network sync, nonce reservation, retry, or protection against stale snapshots.
Repeated/concurrent builds can reuse a nonce; applications must coordinate it.

### GraphQL over stdlib HTTP

```python
from w3sper import Client

node = Client("http://127.0.0.1:8080", timeout=10)
height = node.block_height()       # exact JSON integer or decimal-string height
chain_id = node.node_info().chain_id
raw_data = node.query("block(height: -1) { header { height } }")
```

Requests POST JSON to `<base path>/graphql`, with `rusk-version: 1.0.0-rc.0`
(overridable). Selection sets are wrapped; full documents and `variables={...}`
are supported. `query()` raises `GraphQLError` (with errors and partial data),
`HTTPStatusError`, or `NetworkError` for malformed responses. urllib transport
errors propagate. Missing/malformed node fields raise instead of silently
becoming chain/height zero. `query_raw()` checks HTTP status/nonempty body only.
Use HTTPS for remote nodes.

## Scope and lifecycle

**Supported:** values/encodings, public profiles, deterministic profile
derivation, offline Moonlight value-transfer signing, caller-backed balances,
and minimal GraphQL HTTP queries.

**Not supported:** Phoenix sync/balances/transfers/proving, staking, deployment,
contract data drivers/payloads, RUES/WebSockets, or live preverify/propagate and
confirmation. Phoenix destinations/balances raise `NotImplementedError`.

Use `WalletCore` as a context manager or call idempotent `.close()`. One lock
serializes each instance, including output copying/freeing and close. Outputs
survive later calls and close. Operations after close raise `ClosedError`;
nonzero WASM statuses raise `CodeError(operation, code)`. A runtime trap or
invalid output ABI closes the instance. Use independent instances for parallel
signing; each instance compiles its own copy of the module.

## Checks

```sh
python -m pip install -e . build
python -m unittest discover -s tests -v
python -m build
python examples/offline.py
```

Tests exercise real WASM, full signed bytes/hashes, malformed boundaries, error
statuses, exact allocation/free sizes, output ownership, concurrency, lifecycle,
and a local HTTP server. See [test vectors](tests/fixtures/README.md) for the
cross-language fixtures and how to regenerate them.
CI builds both package formats and tests the installed wheel outside the source tree.

## License

[Mozilla Public License 2.0](LICENSE). See [NOTICE.md](NOTICE.md) for third-party notices.
