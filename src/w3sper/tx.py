# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""Immutable wallet orchestration and every wallet-core transaction family."""
from contextlib import ExitStack
from dataclasses import dataclass, field, replace

from ._validation import octets, uint
from .cancellation import Budget, validate_timeout
from .encoding import base58_decode
from .payload import ContractCall, DeployData, Memo, nonempty
from .profile import Profile, ProfileSigner, SigningProfile
from .state import AccountBalance, AddressBalance, StakeInfo
from .sync import account_bytes, AddressSyncer
from .values import Gas, validate_chain_id
from .walletcore import WalletCore


@dataclass(frozen=True)
class TransferResult:
    buffer: bytes
    hash: str
    chain_id: int
    nonce: int | None = None
    nullifiers: tuple[bytes, ...] = ()
    contract_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "buffer", octets(self.buffer, "transaction"))
        object.__setattr__(self, "nullifiers", tuple(octets(n, "nullifier", 32) for n in self.nullifiers))
        validate_chain_id(self.chain_id)
        if self.nonce is not None:
            uint(self.nonce, 64, "transaction nonce")

    def __bytes__(self):
        return self.buffer


class Bookkeeper:
    """Caller-owned treasury snapshots, not automatic persistence/nonce reservation.

    A Treasury implements account/address/stake_info/stake_keys with read options.
    The legacy one-argument account_balance callback is still supported.
    """
    def __init__(self, core: WalletCore, account_balance=None, *, treasury=None,
                 address_notes=None, stake_info=None, stake_keys=None, timeout=120):
        self.core = core
        self.timeout = validate_timeout(timeout)
        self._treasury = treasury
        self._callbacks = {"account": account_balance, "address": address_notes,
                           "stake_info": stake_info, "stake_keys": stake_keys}

    def _read(self, name, identifier, budget):
        budget.check()
        if self._treasury is not None:
            result = getattr(self._treasury, name)(identifier, cancel=budget.token,
                                                  timeout=budget.remaining() or 0)
        else:
            callback = self._callbacks[name]
            if callback is None:
                label = "account_balance" if name == "account" else name
                raise ValueError(f"a {label} callback or treasury is required")
            result = callback(identifier)
        budget.check()
        return result

    def balance(self, identifier, *, timeout=..., cancel=None, _budget=None):
        own = _budget is None
        budget = Budget(self.timeout if timeout is ... else timeout, cancel) if own else _budget
        try:
            if isinstance(identifier, ProfileSigner):
                identifier.assert_alive()
                notes = self._read("address", identifier.address, budget)
                with identifier._keys("view") as key:
                    result = AddressBalance(*self.core.balance_with_key(key, notes))
            else:
                if isinstance(identifier, str) and len(base58_decode(identifier)) == 64:
                    raise TypeError("a ProfileSigner is required for address balance")
                result = self._read("account", account_bytes(identifier), budget)
                if not isinstance(result, AccountBalance):
                    raise TypeError("account_balance must return AccountBalance")
            budget.check()
            return result
        finally:
            if own:
                budget.close()

    @property
    def minimum_stake(self):
        return self.core.globals.minimum_stake

    def stake_info(self, identifier, *, timeout=..., cancel=None, _budget=None):
        own = _budget is None
        budget = Budget(self.timeout if timeout is ... else timeout, cancel) if own else _budget
        try:
            result = self._read("stake_info", account_bytes(identifier), budget)
            if not isinstance(result, StakeInfo):
                raise TypeError("stake_info must return StakeInfo")
            return result
        finally:
            if own:
                budget.close()

    def stake_keys(self, identifier, *, timeout=..., cancel=None, _budget=None):
        own = _budget is None
        budget = Budget(self.timeout if timeout is ... else timeout, cancel) if own else _budget
        try:
            import copy
            return copy.deepcopy(self._read("stake_keys", account_bytes(identifier), budget))
        finally:
            if own:
                budget.close()

    def stake_owner(self, identifier, **options):
        keys = self.stake_keys(identifier, **options)
        return keys["owner"] if keys is not None else None

    def pick(self, signer, amount, *, timeout=..., cancel=None, _budget=None):
        if not isinstance(signer, ProfileSigner):
            raise TypeError("a ProfileSigner is required to pick notes")
        uint(amount, 64, "pick amount")
        own = _budget is None
        budget = Budget(self.timeout if timeout is ... else timeout, cancel) if own else _budget
        try:
            signer.assert_alive()
            notes = self._read("address", signer.address, budget)
            with signer._keys("view") as key:
                _, spendable = self.core.balance_with_key(key, notes)
                if spendable < amount:
                    raise ValueError("insufficient spendable Phoenix funds")
                result = self.core.pick_notes_with_key(key, notes, amount)
            budget.check()
            return result
        finally:
            if own:
                budget.close()

    def bind(self, profile):
        if not isinstance(profile, (Profile, ProfileSigner)):
            raise TypeError("a Profile or ProfileSigner is required")
        return BookEntry(profile, self)


@dataclass(frozen=True)
class BookEntry:
    profile: Profile | ProfileSigner
    bookkeeper: Bookkeeper

    @property
    def public_profile(self):
        return self.profile.profile if isinstance(self.profile, ProfileSigner) else self.profile

    @property
    def signer(self):
        return self.profile if isinstance(self.profile, ProfileSigner) else None

    def balance(self, kind="account", **options):
        if kind not in ("account", "address"):
            raise ValueError("balance kind must be account or address")
        if kind == "address" and self.signer is None:
            raise TypeError("a ProfileSigner is required for address balance")
        return self.bookkeeper.balance(self.signer if kind == "address" else self.public_profile, **options)

    def stake_info(self, **options):
        return self.bookkeeper.stake_info(self.public_profile, **options)

    def transfer(self, amount=0):
        return Transfer(self).amount(amount)

    def shield(self, amount):
        return ShieldTransfer(self).amount(amount)

    def unshield(self, amount):
        return UnshieldTransfer(self).amount(amount)

    def deploy(self, bytecode, *, init_args=b"", deploy_nonce=0, owner=None, payment="account"):
        builder = ContractDeployment(self, _bytecode=nonempty(bytecode, "bytecode"))
        builder = builder.init_args(init_args).deploy_nonce(deploy_nonce)._set_payment(payment)
        return builder if owner is None else builder.owner(owner)

    def stake(self, amount, **options):
        return StakeTransfer(self).amount(amount)._stake_options(**options)

    def topup(self, amount, **options):
        return StakeTransfer(self, _kind="topup").amount(amount)._stake_options(**options)

    def unstake(self, amount=None, **options):
        builder = UnstakeTransfer(self)._stake_options(**options)
        return builder if amount is None else builder.amount(amount)

    def withdraw(self, amount, **options):
        return WithdrawStakeRewardTransfer(self).amount(amount)._stake_options(**options)

    def contract(self, contract_id, network=None, driver=None):
        from .contract import Contract
        return Contract(contract_id, driver=driver, network=network, bookentry=self)


@dataclass(frozen=True)
class Transfer:
    """Immutable builder. nonce(n) is CURRENT nonce (the result signs with n+1).

    Phoenix selection includes transfer + deposit + maximum fee. Spent notes and
    account nonces are never reserved automatically. Re-sync after execution.
    """
    entry: BookEntry
    _kind: str = "transfer"
    _amount: int | None = 0
    _deposit: int = 0
    _gas: Gas = field(default_factory=Gas)
    _chain: int | None = None
    _nonce: int | None = None
    _receiver: bytes | None = field(default=None, repr=False)
    _payload: Memo | ContractCall | DeployData | None = field(default=None, repr=False)
    _committed: bool = False
    _payment: str = "account"
    _owner: ProfileSigner | None = field(default=None, repr=False)
    _owners: tuple[ProfileSigner, ...] = field(default=(), repr=False)
    _bytecode: bytes = field(default=b"", repr=False)
    _init_args: bytes | None = field(default=b"", repr=False)
    _deploy_nonce: int = 0
    _deploy_owner: bytes | None = field(default=None, repr=False)

    def amount(self, value):
        return replace(self, _amount=uint(value, 64, "amount"))

    def deposit(self, value):
        return replace(self, _deposit=uint(value, 64, "deposit"))

    def gas(self, config):
        if not isinstance(config, Gas):
            raise TypeError("gas must be a Gas configuration")
        return replace(self, _gas=config)

    def chain(self, chain_id):
        return replace(self, _chain=validate_chain_id(chain_id))

    def nonce(self, current_nonce):
        return replace(self, _nonce=uint(current_nonce, 64, "current nonce"))

    def to(self, identifier):
        receiver = base58_decode(identifier) if isinstance(identifier, str) else octets(identifier, "recipient")
        if len(receiver) not in (64, 96):
            raise ValueError("recipient must be an account or address")
        return replace(self, _receiver=receiver, _payment="address" if len(receiver) == 64 else "account")

    def memo(self, value):
        return self.payload(Memo(value))

    def payload(self, value):
        if not isinstance(value, (Memo, ContractCall, DeployData)):
            raise TypeError("payload must be Memo, ContractCall or DeployData")
        return replace(self, _payload=value)

    def committed(self):
        return replace(self, _committed=True)

    def obfuscated(self):
        return self  # Wallet-core always obfuscates value-bearing Phoenix transfers.

    def _set_payment(self, payment):
        if payment not in ("account", "address"):
            raise ValueError("payment must be account or address")
        return replace(self, _payment=payment)

    def public(self):
        return self._set_payment("account")

    def shielded(self):
        return self._set_payment("address")

    def owner(self, signer):
        if not isinstance(signer, ProfileSigner):
            raise TypeError("stake owners require a ProfileSigner")
        signer.assert_alive()
        return replace(self, _owner=signer)

    def owner_signers(self, signers):
        signers = tuple(signers)
        for signer in signers:
            if not isinstance(signer, ProfileSigner):
                raise TypeError("stake owners require ProfileSigners")
            signer.assert_alive()
        return replace(self, _owners=signers)

    def _stake_options(self, *, owner_signer=None, owner_signers=(), payment="account"):
        builder = self._set_payment(payment).owner_signers(owner_signers)
        return builder if owner_signer is None else builder.owner(owner_signer)

    def _stake_context(self, signer, budget):
        book = self.entry.bookkeeper
        info = book.stake_info(signer.account, _budget=budget)
        has_stake = info.amount is not None
        value = self._amount
        if self._kind == "stake":
            if has_stake:
                raise ValueError("stake already exists; use topup")
            if value < book.minimum_stake:
                raise ValueError("stake amount is below minimum stake")
        elif self._kind == "topup":
            if not has_stake:
                raise ValueError("no stake to topup")
            if not value:
                raise ValueError("topup must be positive")
        elif self._kind == "unstake":
            if not has_stake:
                raise ValueError("no stake to unstake")
            value = info.amount.total if value is None else value
            if value == 0 or value > info.amount.total:
                raise ValueError("unstake amount must be positive and no greater than stake")
            remaining = info.amount.total - value
            if 0 < remaining < book.minimum_stake:
                raise ValueError("remaining stake is below minimum stake")
        elif self._kind == "stake_reward":
            if value == 0 or value > info.reward:
                raise ValueError("withdrawal must be positive and no greater than reward")
        candidates = tuple(s for s in (self._owner, *self._owners, signer) if s is not None)
        for candidate in candidates:
            candidate.assert_alive()
            if not signer.same_source_of(candidate):
                raise ValueError("stake owner must belong to the same profile source")
        if self._kind == "stake":
            if self._owner is None and self._owners:
                raise ValueError("owner_signers cannot select a new stake owner; use owner")
            owner = self._owner or signer
        else:
            keys = book.stake_keys(signer.account, _budget=budget)
            fund_owner = keys.get("owner") if isinstance(keys, dict) else None
            if not isinstance(fund_owner, dict):
                raise ValueError("stake owner key is unavailable")
            if "Contract" in fund_owner:
                raise ValueError("contract-owned stakes cannot be managed by wallet keys")
            account = account_bytes(fund_owner.get("Account"))
            if self._owner is not None and self._owner.account != account:
                raise ValueError("stake owner does not match on-chain owner")
            owner = next((candidate for candidate in candidates if candidate.account == account), None)
            if owner is None:
                raise ValueError("stake owner key is unavailable")
        return value, owner

    def build(self, network=None, *, timeout=..., cancel=None, _budget=None):
        signer = self.entry.signer
        if signer is None:
            raise ValueError("public profiles cannot sign; a ProfileSigner is required")
        signer.assert_alive()
        if self._kind != "transfer":
            if self._payload is not None or self._deposit or self._receiver is not None:
                raise ValueError(f"{self._kind} does not accept extra payload, deposit or recipient")
        if self._kind == "shield" and self._payment != "account":
            raise ValueError("shielding must be funded from the account")
        if self._kind == "unshield" and self._payment != "address":
            raise ValueError("unshielding must be funded from the address")
        if self._payment == "address" and self._nonce is not None:
            raise ValueError("Phoenix transactions do not use an account nonce")
        book, core = self.entry.bookkeeper, self.entry.bookkeeper.core
        if core is None:
            raise ValueError("a WalletCore instance is required")
        own = _budget is None
        default = network.timeout if network is not None else 0
        budget = Budget(default if timeout is ... else timeout, cancel) if own else _budget
        with ExitStack() as stack:
            if own:
                stack.callback(budget.close)
            stack.callback(signer._cancel.add_callback(lambda: budget.token.cancel(Budget._reason(signer._cancel))))
            budget.check()
            chain = self._chain
            if chain is None:
                if network is None:
                    raise ValueError("chain id is required")
                chain = network.node_info(_budget=budget).chain_id
            shielded = self._kind == "unshield" or (self._kind != "shield" and self._payment == "address")
            value, owner = self._amount, None
            staking = self._kind in ("stake", "topup", "unstake", "stake_reward")
            if staking:
                value, owner = self._stake_context(signer, budget)
                stack.callback(owner._cancel.add_callback(lambda: budget.token.cancel(Budget._reason(owner._cancel))))
            payload = self._payload
            receiver = self._receiver
            contract_id = None
            if self._kind == "deploy":
                payload = DeployData(self._bytecode, self._deploy_owner or signer.account, self._init_args, self._deploy_nonce)
                contract_id = core.contract_id(payload.bytecode, payload.owner, payload.nonce)
                receiver = signer.address if shielded else None
                value = 0
            elif self._kind == "transfer" and receiver is None:
                raise ValueError("a receiver is required")
            committed = self._committed or (isinstance(payload, ContractCall) and payload.committed)
            if committed:
                if shielded or self._kind != "transfer" or not isinstance(payload, ContractCall):
                    raise ValueError("committed transactions require a public contract call")
                if value or self._deposit or receiver != signer.account:
                    raise ValueError("committed calls require a zero-value sender account envelope")
                payload, receiver = replace(payload, committed=True), None
            data = core.serialize_payload(payload) if payload is not None else None
            common = dict(chain_id=chain, gas_limit=self._gas.limit, gas_price=self._gas.price)
            nonce, nullifiers = None, ()
            if shielded:
                if network is None:
                    raise ValueError("a network is required for Phoenix proving/openings")
                spend = (value if not staking or self._kind in ("stake", "topup") else 0) + self._deposit + self._gas.total
                uint(spend, 64, "total Phoenix spend")
                picked = book.pick(signer, spend, _budget=budget)
                nullifiers = tuple(picked)
                syncer = AddressSyncer(network)
                openings = syncer.openings(picked, _budget=budget)
                root = syncer.root(_budget=budget)
                signer.assert_alive()
                if staking:
                    with signer._keys("both") as key, owner._keys("bls") as owner_key:
                        keys = key + owner_key
                        try:
                            tx, circuits = core.stake_transaction("stake" if self._kind == "topup" else self._kind,
                                keys, value, inputs=picked, openings=openings, root=root, **common)
                        finally:
                            keys[:] = bytes(len(keys))
                elif self._kind == "unshield":
                    with signer._keys("both") as key:
                        tx, circuits = core.unshield(key, picked, openings, root, value, **common)
                else:
                    with signer._keys("phoenix") as key:
                        tx, circuits = core.phoenix_with_key(key, receiver, picked, openings, root,
                                                           value, deposit=self._deposit, data=data, **common)
                budget.check()
                proof = network.prove(circuits, _budget=budget)
                budget.check()
                buffer, digest = core.into_proven(tx, proof)
            else:
                current = self._nonce
                if current is None:
                    current = self.entry.balance(_budget=budget).nonce
                nonce = uint(current + 1, 64, "next nonce")
                uint((value if self._kind not in ("unstake", "stake_reward") else 0) + self._deposit + self._gas.total,
                     64, "total public spend")
                if staking:
                    with signer._keys("bls") as key, owner._keys("bls") as owner_key:
                        keys = key + owner_key
                        try:
                            buffer, digest = core.stake_transaction("stake" if self._kind == "topup" else self._kind,
                                                                   keys, value, nonce=nonce, **common)
                        finally:
                            keys[:] = bytes(len(keys))
                elif self._kind == "shield":
                    with signer._keys("both") as key:
                        buffer, digest = core.shield(key, value, nonce=nonce, **common)
                else:
                    with signer._keys("bls") as key:
                        buffer, digest = core.moonlight_with_key(key, receiver, value, nonce=nonce,
                                                                deposit=self._deposit, data=data, **common)
            budget.check()
            signer.assert_alive()
            if owner:
                owner.assert_alive()
            return TransferResult(buffer, digest, chain, nonce, nullifiers, contract_id)


@dataclass(frozen=True)
class ShieldTransfer(Transfer):
    _kind: str = "shield"


@dataclass(frozen=True)
class UnshieldTransfer(Transfer):
    _kind: str = "unshield"
    _payment: str = "address"


@dataclass(frozen=True)
class StakeTransfer(Transfer):
    _kind: str = "stake"


@dataclass(frozen=True)
class UnstakeTransfer(Transfer):
    _kind: str = "unstake"
    _amount: int | None = None


@dataclass(frozen=True)
class WithdrawStakeRewardTransfer(Transfer):
    _kind: str = "stake_reward"


@dataclass(frozen=True)
class ContractDeployment(Transfer):
    _kind: str = "deploy"

    def init_args(self, value):
        return replace(self, _init_args=None if value is None else octets(value, "initialization arguments"))

    def deploy_nonce(self, value):
        return replace(self, _deploy_nonce=uint(value, 64, "deployment nonce"))

    def nonce(self, value):
        return self.deploy_nonce(value)

    def account_nonce(self, value):
        return replace(self, _nonce=uint(value, 64, "current account nonce"))

    def owner(self, value):
        return replace(self, _deploy_owner=nonempty(value, "deployment owner"))
