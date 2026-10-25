# SPDX-License-Identifier: MPL-2.0
"""Live parity acceptance against a disposable, funded local Rusk node.

Requires FOUR exclusively assigned profiles: sender, receiver, owner, reward
fixture. Never point this synthetic-seed test at production funds.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time

from w3sper import (
    AccountSyncer, AddressSyncer, Bookkeeper, Contract, ContractCall, Gas, Generator,
    HTTPStatusError, Network, STAKE, TRANSFER, Treasury, WalletCore,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=os.getenv("W3SPER_NODE_URL"))
    parser.add_argument("--seed", default=os.getenv("W3SPER_SEED"), help="64-byte synthetic seed in hex")
    parser.add_argument("--index", type=int, default=int(os.getenv("W3SPER_PROFILE_INDEX", "20")))
    parser.add_argument("--driver-dir", default=os.getenv("W3SPER_DRIVER_DIR"), required=not os.getenv("W3SPER_DRIVER_DIR"))
    parser.add_argument("--contract", default=os.getenv("W3SPER_TEST_CONTRACT"), help="reference bob.wasm with u8 init/value/reset ABI")
    parser.add_argument("--committed-contract", default=os.getenv("W3SPER_COMMITTED_CONTRACT"),
                        help="host-backed B-ABI target with 32-byte init/run/value")
    parser.add_argument("--groups", default="public,phoenix,conversion,contract,deploy,committed,stake,reward")
    parser.add_argument("--output", help="write incremental JSON results here as well as stdout")
    args = parser.parse_args()
    if not args.node or not args.seed:
        parser.error("--node and --seed (or corresponding environment variables) are required")
    groups = set(args.groups.split(","))
    report = {"url": args.node, "profile_index": args.index, "groups": sorted(groups), "checks": [], "success": False}
    def save():
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    def record(name, **detail):
        check = {"name": name, **detail}
        report["checks"].append(check)
        save()
        print(name, file=sys.stderr, flush=True)
        return check
    try:
        with WalletCore() as core, Generator(bytes.fromhex(args.seed), core) as gen, Network(args.node, core=core, timeout=240) as network:
            sender, receiver, owner, rewards = [gen.derive(args.index + i) for i in range(4)]
            signers = (sender, receiver, owner, rewards)
            for name, identifier in (("stake", STAKE), ("transfer", TRANSFER)):
                path = Path(args.driver_dir) / f"dusk_{name}_contract_dd_opt.wasm"
                if not path.exists():
                    path = Path(args.driver_dir) / f"{name}-driver.wasm"
                network.data_drivers.register(identifier, path)
            network.connect()
            network.ping()
            network.ping_websocket(timeout=5)
            info = network.node_info()
            start_height = network.block_height()
            record("metadata_and_pings", chain_id=info.chain_id, height=start_height, node=info.data,
                   gas_price=network.blocks.gas_price())
            with network.blocks.subscribe("statechange", timeout=10) as changes:
                block = network.blocks.once("accepted", timeout=10)
                assert block.payload["header"]["height"] > start_height
                assert block.origin.id == block.payload["header"]["hash"]
                with network.budget(10) as budget:
                    while True:
                        changed = changes.get(timeout=budget.remaining())
                        if changed.origin.id == block.origin.id and changed.payload["state"] == "finalized":
                            break
                record("block_events", accepted_origin=str(block.origin), height=block.payload["header"]["height"],
                       finalized_origin=str(changed.origin), finalized=changed.payload)
            treasury = Treasury().sync(network, signers)
            book = Bookkeeper(core, treasury=treasury)
            entries = [book.bind(s) for s in signers]
            entry, recipient, _, reward_entry = entries
            def state():
                return [{"index": s.index, "account": asdict(e.balance()), "address": asdict(e.balance("address")),
                         "stake": asdict(e.stake_info()), "stake_keys": book.stake_keys(s.account)}
                        for s, e in zip(signers, entries)]
            report["initial_state"] = state()
            def refresh():
                treasury.sync(network, signers)
            def submit(name, builder, *, payment="account", payer=sender, account=None, address=None, stake=None, reward=None):
                before = state()
                result, event = network.execute_and_wait(builder, timeout=240)
                error = event.payload.get("err")
                refresh()
                after = state()
                expected = {s.index: {"account": (account or {}).get(s.index, 0),
                                     "address": (address or {}).get(s.index, 0),
                                     "nonce": 0, "stake": (stake or {}).get(s.index, 0),
                                     "reward": (reward or {}).get(s.index, 0)} for s in signers}
                expected[payer.index][payment] -= event.gas_paid
                expected[payer.index]["nonce"] = int(payment == "account")
                check = record(name, hash=result.hash, nonce=result.nonce, nullifiers=[n.hex() for n in result.nullifiers],
                               contract_id=result.contract_id, gas_paid=event.gas_paid, payer=payer.index, payment=payment,
                               execution_error=error, block_height=event.payload["block_height"],
                               type=event.payload["inner"]["type"], before=before, after=after,
                               expected_deltas=expected, verified_effects=False)
                assert error is None, f"{name}: unexpected execution error: {error}"
                def stake_total(snapshot):
                    amount = snapshot["stake"]["amount"]
                    return amount["value"] + amount["locked"] if amount else 0
                for old, new in zip(before, after):
                    index = old["index"]
                    actual = {"account": new["account"]["value"] - old["account"]["value"],
                              "address": new["address"]["value"] - old["address"]["value"],
                              "nonce": new["account"]["nonce"] - old["account"]["nonce"],
                              "stake": stake_total(new) - stake_total(old),
                              "reward": new["stake"]["reward"] - old["stake"]["reward"]}
                    assert actual == expected[index], f"{name}, profile {index}: {actual} != {expected[index]}"
                    for field in ("faults", "hard_faults"):
                        assert new["stake"][field] == old["stake"][field]
                check["verified_effects"] = True
                save()
                return result, event
            def reject(name, operation):
                try:
                    operation()
                except (HTTPStatusError, ValueError) as exc:
                    record(name, rejected=True, error_type=type(exc).__name__, error=str(exc))
                else:
                    raise AssertionError(name + " unexpectedly accepted")
            gas = Gas(500_000_000, 1)
            if "public" in groups:
                before = [e.balance() for e in entries[:2]]
                tx, event = submit("public_transfer_memo", entry.transfer(123).to(receiver.account).memo("Python parity 🌒").gas(gas),
                                   account={sender.index: -123, receiver.index: 123})
                assert event.memo(as_text=True) == "Python parity 🌒"
                assert entry.balance().value == before[0].value - 123 - event.gas_paid
                assert recipient.balance().value == before[1].value + 123
                reject("public_replay_rejected", lambda: network.transactions.preverify(tx))
                # Ingress preverification is not full execution: use a genuinely
                # invalid signature, rather than assuming wrong-chain ingress fails.
                fresh = entry.transfer(1).to(receiver.account).gas(gas).build(network)
                corrupted = fresh.buffer[:-1] + bytes([fresh.buffer[-1] ^ 1])
                reject("public_invalid_signature_rejected", lambda: network.transactions.preverify(corrupted))
            if "phoenix" in groups:
                before = [e.balance("address") for e in entries[:2]]
                tx, event = submit("phoenix_transfer_memo", entry.transfer(321).to(receiver.address).memo("Python shielded 🌒").gas(gas),
                                   payment="address", address={sender.index: -321, receiver.index: 321})
                assert entry.balance("address").value == before[0].value - 321 - event.gas_paid
                assert recipient.balance("address").value == before[1].value + 321
                assert event.memo(as_text=True) == "Python shielded 🌒"
                assert all(note["type"] == 1 for note in event.payload["inner"]["outputs"])
                assert set(AddressSyncer(network).spent(tx.nullifiers)) == set(tx.nullifiers)
                reject("phoenix_double_spend_rejected", lambda: network.transactions.preverify(tx))
                fresh = entry.transfer(1).to(receiver.address).gas(gas).build(network)
                corrupted = fresh.buffer[:-1] + bytes([fresh.buffer[-1] ^ 1])
                reject("phoenix_corrupted_transaction_rejected", lambda: network.transactions.preverify(corrupted))
            if "conversion" in groups:
                public, private = entry.balance().value, entry.balance("address").value
                _, event = submit("shield", entry.shield(12345).gas(gas),
                                  account={sender.index: -12345}, address={sender.index: 12345})
                assert entry.balance().value == public - 12345 - event.gas_paid
                assert entry.balance("address").value == private + 12345
                public, private = entry.balance().value, entry.balance("address").value
                _, event = submit("unshield", entry.unshield(54321).gas(gas), payment="address",
                                  account={sender.index: 54321}, address={sender.index: -54321})
                assert entry.balance().value == public + 54321
                assert entry.balance("address").value == private - 54321 - event.gas_paid
            if "contract" in groups:
                transfer = entry.contract(TRANSFER, network)
                stake = Contract(STAKE, network=network)
                assert transfer.call("chain_id") == info.chain_id
                assert int(stake.call("get_version")) == 8
                encoded = transfer.encode("sync_accounts", [0, 5])
                assert encoded == bytes(8) + (5).to_bytes(8, "little")
                record("contract_reads_drivers", transfer_schema=transfer.schema()["name"], stake_schema=stake.schema()["name"],
                       transfer_version=transfer.version(), stake_version=stake.version())
                for name, destination in (("public_contract_deposit", sender.account), ("phoenix_contract_deposit", sender.address)):
                    with transfer.events("deposit").subscribe(timeout=10) as events:
                        payment = "account" if name.startswith("public") else "address"
                        submit(name, transfer.tx("deposit", 2).deposit(2).to(destination).gas(gas),
                               payment=payment, **{payment: {sender.index: -2}})
                        decoded = events.get(timeout=10)
                        assert int(decoded["value"]) == 2
                        record(name + "_decoded_event", event=decoded)
            if "deploy" in groups:
                if not args.contract:
                    raise ValueError("--contract bob.wasm is required for deploy acceptance")
                code = Path(args.contract).read_bytes()
                for label, payment, init in (("public", "account", 7), ("phoenix", "address", 8)):
                    deploy_nonce = time.time_ns() & ((1 << 64) - 1)
                    builder = entry.deploy(code, init_args=bytes([init]), deploy_nonce=deploy_nonce,
                                           owner=owner.account, payment=payment).gas(Gas(500_000_000, 2000))
                    tx, _ = submit(label + "_deploy", builder, payment=payment)
                    target = network.contracts.with_id(tx.contract_id)
                    assert target.call("value") == bytes([init])
                    payload = ContractCall(tx.contract_id, "reset", bytes([init + 1]))
                    call = entry.transfer(0).to(sender.account if payment == "account" else sender.address).payload(payload).gas(gas)
                    submit(label + "_deployed_contract_call", call, payment=payment)
                    assert target.call("value") == bytes([init + 1])
                    record(label + "_deployment_initialized_and_called", contract_id=tx.contract_id, value=init + 1)
            if "committed" in groups:
                if not args.committed_contract:
                    raise ValueError("--committed-contract B-ABI target is required")
                code = Path(args.committed_contract).read_bytes()
                tx, _ = submit("committed_target_deploy", entry.deploy(code, init_args=bytes([7]) * 32,
                               deploy_nonce=time.time_ns() & ((1 << 64) - 1)).gas(Gas(500_000_000, 2000)))
                target = network.contracts.with_id(tx.contract_id)
                assert target.call("value") == bytes([7]) * 32
                payload = ContractCall(tx.contract_id, "run", bytes([9]) * 32)
                _, event = submit("committed_call", entry.transfer(0).to(sender.account).payload(payload).committed().gas(gas))
                assert event.payload["inner"]["is_committed_call"] is True
                assert target.call("value") == bytes([9]) * 32
                record("committed_call_persisted", contract_id=tx.contract_id, value=(bytes([9]) * 32).hex())
            if "stake" in groups:
                for label, stake_entry, stake_signer, payment in (("public", entry, sender, "account"), ("phoenix", recipient, receiver, "address")):
                    opts = {"owner_signer": owner, "payment": payment}
                    assert stake_entry.stake_info().amount is None
                    submit(label + "_stake_distinct_owner", stake_entry.stake(book.minimum_stake, **opts).gas(gas),
                           payment=payment, payer=stake_signer, stake={stake_signer.index: book.minimum_stake},
                           **{payment: {stake_signer.index: -book.minimum_stake}})
                    keys = book.stake_keys(stake_signer.account)
                    assert keys["owner"] == {"Account": owner.account_id}
                    reject(label + "_wrong_stake_owner_rejected", lambda: stake_entry.topup(1, owner_signer=stake_signer, payment=payment).gas(gas).build(network))
                    submit(label + "_stake_topup", stake_entry.topup(123, **opts).gas(gas), payment=payment,
                           payer=stake_signer, stake={stake_signer.index: 123}, **{payment: {stake_signer.index: -123}})
                    assert stake_entry.stake_info().amount.total == book.minimum_stake + 123
                    submit(label + "_partial_unstake", stake_entry.unstake(123, **opts).gas(gas), payment=payment,
                           payer=stake_signer, stake={stake_signer.index: -123}, **{payment: {stake_signer.index: 123}})
                    assert stake_entry.stake_info().amount.total == book.minimum_stake
                    submit(label + "_full_unstake", stake_entry.unstake(**opts).gas(gas), payment=payment,
                           payer=stake_signer, stake={stake_signer.index: -book.minimum_stake},
                           **{payment: {stake_signer.index: book.minimum_stake}})
                    assert stake_entry.stake_info().amount is None
                    assert book.stake_keys(stake_signer.account) is None
            if "reward" in groups:
                initial_reward = reward_entry.stake_info().reward
                for payment in ("account", "address"):
                    submit(payment + "_reward_withdrawal", reward_entry.withdraw(123, owner_signer=owner, payment=payment).gas(gas),
                           payment=payment, payer=rewards, reward={rewards.index: -123}, **{payment: {rewards.index: 123}})
                assert reward_entry.stake_info().reward == initial_reward - 246
            # Archive updates follow block finalization. Wait for observable entries.
            expected = {c["hash"] for c in report["checks"] if c.get("nonce") is not None and c.get("hash")}
            histories = {}
            deadline = time.monotonic() + 45
            while True:
                for signer in (sender, receiver, rewards):
                    with AccountSyncer(network).history(signer, from_height=start_height) as stream:
                        histories[str(signer.index)] = list(stream)
                observed = {e["hash"] for entries_ in histories.values() for e in entries_}
                if expected <= observed:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"archive missing public transactions: {sorted(expected - observed)}")
                time.sleep(1)
            record("account_history", entries=histories)
            report["final_state"] = state()
            report["success"] = True
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        import traceback
        traceback.print_exc(file=sys.stderr)
    finally:
        save()
        print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
