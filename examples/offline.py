# SPDX-License-Identifier: MPL-2.0
"""Run after installation: python examples/offline.py. Never fund this public seed."""

from w3sper import Bookkeeper, Generator, WalletCore, format_to_dusk, parse_from_dusk

# Public deterministic TEST seed, also used by the unchanged Go SDK's tests.
seed = bytes((i * 7 + 1) & 255 for i in range(64))

with WalletCore() as core:
    profiles = Generator(seed, core)
    sender = profiles.default()
    receiver = profiles.next().profile
    result = (
        Bookkeeper(core).bind(sender).transfer()
        .amount(parse_from_dusk("1"))
        .chain(42)
        .nonce(0)  # CURRENT account nonce: the signed transaction uses 1.
        .to(receiver.account_id)
        .build()
    )

# Results are owned Python bytes and remain usable after closing the WASM.
assert result.hash == "072cd70a7819c53f25028f5cb4f937b427823b5474d15871c4e309448fc5d133"
print(f"Signed {format_to_dusk(parse_from_dusk('1'))} DUSK offline; no submission")
print(f"{len(result.buffer)} bytes, chain={result.chain_id}, nonce={result.nonce}")
print(f"hash={result.hash}")
