# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""Account/history and Phoenix note synchronization with bounded, closable streams."""
from contextlib import ExitStack
import copy
import json
import struct
from threading import RLock

from . import _rkyv
from ._validation import octets, uint
from .cancellation import Budget, CancellationToken
from .encoding import base58_decode, base58_encode
from .payload import TRANSFER, STAKE
from .profile import Profile, ProfileSigner
from .state import AccountBalance, AddressBalance, StakeInfo
from .transport import NetworkError
from .values import Bookmark


def account_bytes(identifier):
    if isinstance(identifier, (Profile, ProfileSigner)):
        identifier = identifier.account
    if isinstance(identifier, str):
        identifier = base58_decode(identifier)
    return octets(identifier, "account", 96)


class ReadStream:
    """Single-consumer iterator; use a with block or close when stopping early."""
    def __init__(self, iterator, budget, *, own_budget=True, cleanup=None):
        self._iterator, self._budget = iter(iterator), budget
        self._own_budget, self._cleanup = own_budget, cleanup
        self._closed = False
        self._released = False
        self._release_lock = RLock()
        self._remove_abort = lambda: None
        self._remove_abort = budget.token.add_callback(self._release)

    def _release(self):
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._remove_abort()
        try:
            if self._cleanup:
                self._cleanup()
        finally:
            if self._own_budget:
                self._budget.close()

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            self._budget.check()
            value = next(self._iterator)
            self._budget.check()
            return value
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._iterator, "close", None)
            if close:
                close()
        finally:
            self._release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        if hasattr(self, "_closed"):
            self.close()


class AccountSyncer:
    def __init__(self, network):
        self.network = network

    def _read(self, identifiers, contract, method, parser, *, timeout=..., cancel=None, _budget=None):
        own = _budget is None
        budget = self.network.budget(timeout, cancel) if own else _budget
        try:
            budget.check()
            accounts = self.network.core.accounts_into_raw(map(account_bytes, identifiers))
            results = []
            for account in accounts:
                budget.check()
                raw = self.network.contracts.with_id(contract).call(method, account, _budget=budget)
                try:
                    results.append(parser(raw))
                except (TypeError, ValueError, struct.error) as exc:
                    raise NetworkError(f"invalid {method} response") from exc
            budget.check()
            return tuple(results)
        finally:
            if own:
                budget.close()

    def balances(self, identifiers, **options):
        def parse(raw):
            return AccountBalance(*struct.unpack("<QQ", octets(raw, "account state", 16)))
        return self._read(identifiers, TRANSFER, "account", parse, **options)

    def stakes(self, identifiers, **options):
        return self._read(identifiers, STAKE, "get_stake", StakeInfo.parse, **options)

    def stake_keys(self, identifiers, *, timeout=..., cancel=None, _budget=None):
        own = _budget is None
        budget = self.network.budget(timeout, cancel) if own else _budget
        try:
            driver = self.network.data_drivers.get(STAKE, _budget=budget)
            def parse(data):
                value = driver.decode_output_fn("get_stake_keys", data)
                if value is not None:
                    if not isinstance(value, dict) or set(value) != {"account", "owner"}:
                        raise ValueError("invalid stake keys")
                    account_bytes(value["account"])
                    owner = value["owner"]
                    if not isinstance(owner, dict) or len(owner) != 1:
                        raise ValueError("invalid stake owner")
                    if "Account" in owner:
                        account_bytes(owner["Account"])
                    elif "Contract" in owner:
                        from .payload import contract_bytes
                        contract_bytes(owner["Contract"])
                    else:
                        raise ValueError("invalid stake owner kind")
                return value
            return self._read(identifiers, STAKE, "get_stake_keys", parse, _budget=budget)
        finally:
            if own:
                budget.close()

    def history(self, identifier, *, from_height=0, to_height=None, limit=None, order="asc",
                timeout=..., cancel=None):
        """Stream one account's enriched history. All integers retain full precision."""
        account = base58_encode(account_bytes(identifier))
        uint(from_height, 64, "from height")
        if to_height is not None:
            uint(to_height, 64, "to height")
        if limit is not None:
            uint(limit, 32, "history limit")
        if order not in ("asc", "desc"):
            raise ValueError("order must be asc or desc")
        budget = self.network.budget(timeout, cancel)
        def read():
            end = self.network.block_height(_budget=budget) if to_height is None else to_height
            if end < from_height:
                raise ValueError("to height precedes from height")
            query = (f'fullMoonlightHistory(address: "{account}", fromBlock: {from_height}, '
                     f'ord: "{order}", toBlock: {end}) {{ json }}')
            data = self.network.query(query, _budget=budget)
            entries = (data.get("fullMoonlightHistory") or {}).get("json", [])
            if isinstance(entries, str):
                entries = json.loads(entries)
            if not isinstance(entries, list):
                raise NetworkError("invalid account history")
            entries = entries if limit is None else entries[:limit]
            for start in range(0, len(entries), 500):
                batch = entries[start:start + 500]
                queries = []
                for i, entry in enumerate(batch):
                    from .payload import contract_bytes
                    tx_hash = contract_bytes(entry["origin"]).hex()
                    queries.append(f'tx{i}: tx(hash: "{tx_hash}") {{ blockHash blockTimestamp err gasSpent tx {{ callData {{ fnName }} gasLimit gasPrice isDeploy memo }} }}')
                result = self.network.query(" ".join(queries), _budget=budget)
                for i, entry in enumerate(batch):
                    budget.check()
                    yield self._history_entry(account, entry, result[f"tx{i}"])
        return ReadStream(read(), budget)

    @staticmethod
    def _history_entry(account, entry, result):
        sender, receiver, value = "N/A", "N/A", 0
        for event in entry["events"]:
            if event["target"] != TRANSFER:
                continue
            topic, data = event["topic"], event["data"]
            if topic == "convert":
                if (data.get("receiver") or {}).get("Moonlight") == account:
                    value += int(data["value"])
                    sender, receiver = "Phoenix", account
                if data.get("sender") == account:
                    value -= int(data["value"])
                    sender, receiver = account, "Phoenix"
            elif topic == "moonlight":
                if data.get("receiver") is not None:
                    receiver = data["receiver"]
                    if receiver == account:
                        value += int(data["value"])
                sender = data["sender"]
                if sender == account:
                    value -= int(data["value"])
                refund = data.get("refund_info")
                if refund and refund[0] == account and receiver == account:
                    value += int(refund[1])
            elif topic in ("mint", "withdraw"):
                if (data.get("receiver") or {}).get("Moonlight") == account:
                    value += int(data["value"])
            elif topic == "deposit" and data.get("sender") == account:
                value -= int(data["value"])
            elif topic == "contract_to_account" and data.get("receiver") == account:
                value += int(data["value"])
        tx = result["tx"]
        gas_price, gas_spent = int(tx["gasPrice"]), int(result["gasSpent"])
        return {"block_hash": result["blockHash"], "block_height": int(entry["block_height"]),
                "block_timestamp": result["blockTimestamp"] * 1000,
                "fee_paid": gas_price * gas_spent, "from": sender, "to": receiver,
                "gas_limit": int(tx["gasLimit"]), "gas_price": gas_price, "gas_spent": gas_spent,
                "hash": entry["origin"], "memo": tx.get("memo") or "",
                "method": "deploy" if tx["isDeploy"] else (tx.get("callData") or {}).get("fnName", "transfer"),
                "owner": account, "success": result["err"] is None, "type": "public", "value": value}


class AddressSyncer:
    def __init__(self, network, *, chunk_size=512):
        if type(chunk_size) is not int or chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.network, self.chunk_size = network, chunk_size
        self._progress = set()

    def on_progress(self, callback):
        self._progress.add(callback)
        return lambda: self._progress.discard(callback)

    def root(self, **options):
        return octets(self.network.contracts.transfer_contract.call("root", **options), "note root", 32)

    def openings(self, notes, *, timeout=..., cancel=None, _budget=None):
        own = _budget is None
        budget = self.network.budget(timeout, cancel) if own else _budget
        try:
            budget.check()
            bookmarks = self.network.core.bookmarks(notes)
            result = []
            for bookmark in bookmarks:
                result.append(self.network.contracts.transfer_contract.call("opening", bookmark.to_bytes(8, "little"), _budget=budget))
            budget.check()
            return tuple(result)
        finally:
            if own:
                budget.close()

    def spent(self, nullifiers, *, timeout=..., cancel=None, _budget=None):
        nullifiers = tuple(octets(n, "nullifier", 32) for n in nullifiers)
        own = _budget is None
        budget = self.network.budget(timeout, cancel) if own else _budget
        try:
            result = []
            for i in range(0, len(nullifiers), (60 * 1024 - 8) // 32):
                batch = nullifiers[i:i + (60 * 1024 - 8) // 32]
                raw = self.network.contracts.transfer_contract.call("existing_nullifiers", _rkyv.vector(batch, 32), _budget=budget)
                result.extend(_rkyv.vector_items(raw, 32))
            budget.check()
            return tuple(result)
        finally:
            if own:
                budget.close()

    def notes(self, signers, *, from_=0, timeout=..., cancel=None):
        signers = tuple(signers)
        if not 1 <= len(signers) <= 256 or any(not isinstance(s, ProfileSigner) for s in signers):
            raise TypeError("expected 1..256 ProfileSigners")
        if any(not signers[0].same_source_of(s) for s in signers):
            raise ValueError("all note owners must share a profile source")
        if isinstance(from_, Bookmark):
            topic, body = "leaves_from_pos", bytes(from_)
        else:
            topic, body = "leaves_from_height", uint(from_, 64, "from height").to_bytes(8, "little")
        token = CancellationToken()
        stack = ExitStack()
        combined = bytearray()
        try:
            for signer in signers:
                signer.assert_alive()
                stack.callback(signer._cancel.add_callback(lambda s=signer: token.cancel(Budget._reason(s._cancel))))
            if cancel:
                stack.callback(cancel.add_callback(lambda: token.cancel(Budget._reason(cancel))))
            budget = self.network.budget(timeout, token)
            stack.callback(budget.close)
            keys = [stack.enter_context(s._keys("phoenix", check_result=False)) for s in signers]
            combined = bytearray().join(keys)
            stack.callback(lambda: combined.__setitem__(slice(None), bytes(len(combined))))
            count_raw = self.network.contracts.transfer_contract.call("num_notes", _budget=budget)
            last_bookmark = int.from_bytes(octets(count_raw, "note count", 8), "little")
            last_height = self.network.block_height(_budget=budget)
            response = self.network.contracts.transfer_contract.call(topic, body, feeder=True, stream=True, _budget=budget)
            stack.callback(response.close)
            item_size = self.network.core.globals.item_size
            batch_size = item_size * self.chunk_size
        except BaseException:
            stack.close()
            raise
        def read():
            pending = bytearray()
            def mapped(data):
                budget.check()
                for signer in signers:
                    signer.assert_alive()
                owned, info = self.network.core.map_owned_with_keys(combined, data)
                for signer in signers:
                    signer.assert_alive()
                progress = {"owned_count": sum(map(len, owned)),
                            "progress": min((info["bookmark"] + 1) / last_bookmark, 1) if last_bookmark else 1,
                            "bookmarks": {"current": info["bookmark"], "last": last_bookmark},
                            "blocks": {"current": info["block_height"], "last": last_height}}
                for callback in tuple(self._progress):
                    callback(copy.deepcopy(progress))
                return owned, info
            for chunk in response.iter_chunks(batch_size):
                pending.extend(chunk)
                while len(pending) >= batch_size:
                    data = bytes(pending[:batch_size])
                    del pending[:batch_size]
                    yield mapped(data)
            if len(pending) % item_size:
                raise NetworkError("truncated Phoenix feeder entry")
            if pending:
                yield mapped(bytes(pending))
        return ReadStream(read(), budget, own_budget=False, cleanup=stack.close)


class Treasury:
    """Optional in-memory wallet snapshots. Applications own persistence/freshness."""
    def __init__(self):
        self._lock = RLock()
        self._accounts, self._addresses, self._stakes, self._stake_keys = {}, {}, {}, {}

    def account(self, identifier, **options):
        with self._lock:
            return self._accounts[account_bytes(identifier)]

    def address(self, identifier, **options):
        if isinstance(identifier, str):
            identifier = base58_decode(identifier)
        with self._lock:
            return dict(self._addresses.get(octets(identifier, "address", 64), {}))

    def stake_info(self, identifier, **options):
        with self._lock:
            return self._stakes[account_bytes(identifier)]

    def stake_keys(self, identifier, **options):
        with self._lock:
            key = account_bytes(identifier)
            if key not in self._stake_keys:
                raise ValueError("stake owner lookup is unavailable; register the stake data driver and synchronize")
            return copy.deepcopy(self._stake_keys[key])

    def sync(self, network, signers, *, from_=0, accounts=True, addresses=True, timeout=..., cancel=None):
        signers = tuple(signers)
        account_sync = AccountSyncer(network)
        if accounts:
            balances = account_sync.balances(signers, timeout=timeout, cancel=cancel)
            stakes = account_sync.stakes(signers, timeout=timeout, cancel=cancel)
            keys = account_sync.stake_keys(signers, timeout=timeout, cancel=cancel) if network.data_drivers.has(STAKE) else None
            with self._lock:
                for i, signer in enumerate(signers):
                    self._accounts[signer.account] = balances[i]
                    self._stakes[signer.account] = stakes[i]
                    if keys is not None:
                        self._stake_keys[signer.account] = keys[i]
        if addresses:
            syncer = AddressSyncer(network)
            with self._lock:
                notes = [dict(self._addresses.get(s.address, {})) for s in signers]
            with syncer.notes(signers, from_=from_, timeout=timeout, cancel=cancel) as stream:
                for owned, _ in stream:
                    for dest, source in zip(notes, owned):
                        dest.update(source)
            spent = set(syncer.spent([key for group in notes for key in group], timeout=timeout, cancel=cancel))
            with self._lock:
                for signer, group in zip(signers, notes):
                    self._addresses[signer.address] = {k: v for k, v in group.items() if k not in spent}
        return self
