# w3sper.py

A Python SDK for Dusk: Moonlight and Phoenix wallets, transfers, conversions,
staking, contract deployment, WASM data drivers, GraphQL, and RUES events.
Cryptography and transaction serialization run in the bundled Dusk wallet-core
WASM through Wasmtime. WebSocket transport uses `websockets`; HTTP uses the
Python standard library. Python 3.11+ is required.

## Install

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

The wheel includes wallet-core; offline signing needs no node or JavaScript
runtime. See [NOTICE.md](NOTICE.md) for the pinned artifact and licenses.

## Public profiles and signing

```python
from w3sper import Bookkeeper, ProfileGenerator, WalletCore, parse_from_dusk

# PUBLIC TEST SEED. Never use this seed for real funds.
seed = bytes((i * 7 + 1) & 255 for i in range(64))
with WalletCore() as core, ProfileGenerator(seed, core) as profiles:
    public = profiles.default()                  # public identity, index 0
    receiver = profiles.next()                   # public identity, index 1
    with profiles.signer(public) as signer:      # separately owned child keys
        result = (
            Bookkeeper(core).bind(signer).transfer(parse_from_dusk("1"))
            .to(receiver.account).chain(42).nonce(0).build()
        )
print(result.hash)
# 072cd70a7819c53f25028f5cb4f937b427823b5474d15871c4e309448fc5d133
```

`Profile(raw_160_bytes)` contains only an address (64 bytes) and account
(96 bytes). `.address_id` and `.account_id` are Base58 identifiers. Parsing
checks lengths, not curve points; wallet-core validates transaction keys.
Public identities cannot sign or decrypt private balances.

`ProfileGenerator(seed_or_provider, core)` caches public identities. `derive(i)`
accepts 0..255; `default()` is index 0; `next()` starts at 1; `at(i)` only looks
in the cache. Issuing a signer consults the provider again and checks that its
keys match the original identity. Providers are synchronous callables returning
64 bytes. Existing signers do not consult the provider during operations.

`ProfileSigner.sign_bls_message(bytes)` signs exact bytes using Dusk BLS V2,
returning 48 bytes. No prefix, prehash, approval, origin binding, or replay
protection is added. `dispose()`/`close()` rejects new operations and stale
in-flight results while retaining readable public identity. Owned mutable
keys/snapshots and native secret buffers are wiped best-effort; Python/runtime
copies are **not guaranteed to be erased**. Do not pickle or log key material.
Disposal cannot revoke transactions already sent.

For compatibility, `Generator(seed, core)` returns cached signers rather than
public identities; its `close()` disposes them. `SigningProfile` is a
compatibility name for `ProfileSigner`, now retaining child keys, not a seed.
Closing `ProfileGenerator` does not revoke independently issued signers.

## Network and wallet state

This independent example creates fresh live contexts and **submits a Phoenix
transfer**. Set the environment variables to a disposable funded node/wallet
and its stake data-driver file. Never use production funds for examples.

```python
import os
from w3sper import (
    AccountSyncer, Bookkeeper, Network, ProfileGenerator, STAKE, Treasury, WalletCore,
)

seed = bytes.fromhex(os.environ["W3SPER_SEED"])
index = int(os.environ.get("W3SPER_PROFILE_INDEX", "20"))
with WalletCore() as core, ProfileGenerator(seed, core) as profiles:
    receiver = profiles.derive(index + 1)
    with profiles.signer(profiles.derive(index)) as signer, Network(
        os.environ["W3SPER_NODE_URL"], core=core, timeout=120
    ) as network:
        network.data_drivers.register(STAKE, os.environ["W3SPER_STAKE_DRIVER"])
        info = network.node_info()                 # POST /on/node/info
        height = network.block_height()
        balance, = AccountSyncer(network).balances([signer.profile])
        network.connect()                         # real RUES WebSocket session
        network.ping_websocket(timeout=5)          # nonce-matched pong

        treasury = Treasury().sync(network, [signer])
        entry = Bookkeeper(core, treasury=treasury).bind(signer)
        public_balance = entry.balance()
        private_balance = entry.balance("address") # value and spendable
        tx, executed = network.execute_and_wait(
            entry.transfer(123).to(receiver.address).memo("hello")
        )
        if executed.payload["err"] is not None:
            raise RuntimeError(executed.payload["err"])
        treasury.sync(network, [signer])
```

The shorter snippets below belong inside equivalent open network/signer/core
contexts; the scopes above dispose their resources when exited.

`Network` and `Client` name the same synchronous client. HTTP reads do not
require `connect()`. `ping()` probes HTTP, `connected` reports local socket
state, and `ping_websocket()` requires a fresh pong on that socket. `disconnect()`
does not cancel unrelated HTTP reads; `close()` also releases internally owned
wallet-core and registry resources.

`query(selection_or_document, variables=..., operation_name=...)` supports full
GraphQL request dictionaries too. `query_raw()` preserves the response bytes.
`GraphQLError` retains errors and partial data; `HTTPStatusError` retains status
and body. Node metadata, CRS, provisioners and block gas-price statistics are
available through `network.node` and `network.blocks`.

Account sync supports `.balances()`, `.stakes()`, `.stake_keys()` and
`.history(profile, from_height=0, to_height=None, limit=None, order="asc")`.
History is a closable iterator of enriched entries with exact amounts, fees,
block metadata, memo, method and success. Stake owner lookup requires the
stake data driver registered under `STAKE`.

`AddressSyncer` provides `.notes(signers, from_=height_or_bookmark)`, `.spent()`,
`.openings()` and `.root()`. Note streams yield `(owned_maps, sync_info)`, where
maps associate immutable nullifier bytes with archived note-leaf bytes.
`on_progress(callback)` returns a stop function. Scans process bounded batches
without ownership worker threads, preserve partial HTTP chunks, and reject
truncated leaves. Spent queries are batched below the node's request limit.
Always close a stream when stopping early:

```python
from w3sper import AddressSyncer, Bookmark

with AddressSyncer(network).notes([signer], from_=Bookmark.from_int(0)) as stream:
    for owned, cursor in stream:
        print(len(owned[0]), cursor["bookmark"])
```

`Treasury` is optional in-memory storage. Applications own persistence,
snapshot freshness and nonce coordination. A custom treasury supplies
`account`, `address`, `stake_info`, and `stake_keys` read methods. The existing
`Bookkeeper(core, account_balance=callback)` form still works. Neither builders
nor treasury reserve nonces/notes; concurrent builds can spend the same state.

## Transactions

Builders are immutable: retain every fluent method's return value.
`build(network)` signs locally, resolves missing chain/nonce state, and obtains
openings/proofs for Phoenix. `network.execute(builder_or_result)` performs
preverify then propagate; **HTTP admission is not successful execution**.
`execute_and_wait()` subscribes before propagation and returns the execution
event. Check `executed.payload["err"]`. `.gas_paid`, `.memo(as_text=True)` and
`.call()` expose execution details.

- `entry.transfer(amount).to(account_or_address)` selects Moonlight/Phoenix.
  `.memo(text_or_bytes)`, `.deposit(lux)` and `.payload(ContractCall(...))`
  attach native transaction data. Phoenix transfers are always obfuscated.
- `entry.shield(amount)` / `entry.unshield(amount)` convert between the same
  profile's public and private balances.
- `entry.deploy(bytecode, init_args=b"...", deploy_nonce=7, owner=owner_bytes,
  payment="account" or "address")` deploys publicly or privately. Use
  `init_args=None` to omit initialization. Results
  include `contract_id`; `.account_nonce(n)` sets the current public nonce,
  while deployment `.nonce(n)` means the deployment nonce.
- `entry.stake(amount, owner_signer=owner)`, `.topup(amount)`,
  `.unstake(amount=None)` and `.withdraw(amount)` support either payment kind.
  `.owner_signers([...])` resolves an existing on-chain owner. A distinct owner
  must have its own signer from the same generator. Contract-owned stakes
  require contract authority and cannot be managed with wallet keys.
- `.committed()` requires a zero-value/deposit public self-envelope with a
  `ContractCall` payload. The target must support Rusk's host-backed **B ABI**;
  a legacy A-ABI contract is not a valid committed-call target.

`nonce(n)` on a transfer is the **current** nonce; it signs with `n + 1`.
Low-level `WalletCore.moonlight[_with_key](..., nonce=n)` takes the final nonce.
Results contain owned network bytes without host ABI prefixes. Phoenix results
include spent nullifiers. Proof attachment does not itself verify a proof;
node preverification/execution does.

## Contracts and data drivers

```python
from w3sper import Contract, STAKE, TRANSFER

network.data_drivers.register(STAKE, "dusk_stake_contract_dd_opt.wasm")
network.data_drivers.register(TRANSFER, "dusk_transfer_contract_dd_opt.wasm")
transfer = entry.contract(TRANSFER, network)
assert transfer.call("chain_id") == network.node_info().chain_id
encoded = transfer.encode("sync_accounts", ["0", "5"])
print(transfer.schema(), transfer.version())

with transfer.events("deposit").subscribe() as events:
    tx, executed = network.execute_and_wait(
        transfer.tx("deposit", 2).to(signer.account).deposit(2)
    )
    print(events.get(timeout=30))
```

Registry locators can be paths, HTTP(S) URLs, WASM bytes, or synchronous
byte-provider callables. Instances are initialized once, cached, serialized,
and closed by the registry. `DataDriver` exposes `encode_input_fn`,
`decode_input_fn`, `decode_output_fn`, `decode_event`, `get_schema`, and
`get_version`. `.encode(name, value)` accepts Python JSON values; large integers
are exact and driver-defined u64 outputs often use decimal strings. Driver
instances have checked export signatures, bounded output/memory/fuel, typed
native errors, and fail closed on traps or malformed native output.

Contract events can use `.once()`, a context-managed `.subscribe()`, or
`.on(callback)` returning a closable subscription. Raw RUES scopes are available
via `network.rues.scope(name)`, with `.with_id(id)`, `.call(topic, body)`,
`.subscribe(topic)`, `.once(topic)` and `.on(topic, callback)`. Registrations are
shared per session/path; stopping the last consumer sends DELETE. Queues are
bounded and fail on overflow rather than dropping events. Disconnect does not
replay missed events or reconnect automatically.

## Bounds and cancellation

Amounts, nonces and bookmarks are u64; indices and chain IDs are u8. Booleans
and floats are rejected as integers. `Gas(limit=None, price=None)` defaults to
2,900,000,000 and 1; explicit values must be positive u64 and their product must
fit u64. Builders also reject overflowing aggregate spend. Decimal conversions
are exact: no floats, exponents, negative values, rounding, or trailing decimal
point. Malformed payloads are rejected rather than silently omitted.

Network/read timeouts are **seconds**, default 120. Zero disables the deadline;
`None` is invalid. Pass a `CancellationToken` as `cancel=` and call `.cancel()`
from another thread. Deadlines cover headers, bodies and stream consumption;
paused streams release resources on cancellation/expiry. TCP, TLS, WebSocket
upgrade/session handshake, event waits and HTTP bodies are cancellable.

Two synchronous boundaries cannot be forcibly interrupted: the operating
system's `socket.getaddrinfo()` DNS resolver, and application-provided Python
callbacks/providers or synchronous WASM work. Cancellation is checked before
and after these stages; no abandoned request/resolver threads are spawned.
Use an IP literal when a strict connection deadline must avoid system DNS.
Callbacks must return promptly. Data-driver WASM execution has an independent
fuel bound. Registration timeout does not limit a continuous listener's
lifetime; cancellation or `.close()` does.

## Checks and examples

```sh
python -m pip install -e . build
python -m unittest discover -s tests -v
python examples/offline.py
python -m build
```

The tests use the real wallet-core and data-driver modules, fixed JS/Rust
vectors, and local HTTP/WebSocket servers. See [fixtures](tests/fixtures/README.md).
A live acceptance entrypoint exercises funded profiles, public/shielded
transactions, rejection paths, history, staking, deployment and committed calls:

```sh
python examples/live_parity.py --node "$W3SPER_NODE_URL" --seed "$W3SPER_SEED" \
  --index 20 --driver-dir tests/fixtures --contract tests/fixtures/bob.wasm \
  --committed-contract tests/fixtures/committed-target.wasm --output live.json
```

This command submits transactions. It needs a disposable Phoenix-enabled node
funding only the assigned profiles `index..index+3`, with a distinct-owner reward
fixture at `index+3`. It emits JSON results and verifies state effects; never use
it against production funds. Individual families can be selected with `--groups`.

## License

[Mozilla Public License 2.0](LICENSE). Third-party details: [NOTICE.md](NOTICE.md).
