# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""Contract facade over registered WASM data drivers and RUES."""
import base64
import re
from .cancellation import Budget
from .payload import ContractCall, contract_bytes


class ContractEvents:
    def __init__(self, contract, topic):
        self.contract, self.topic = contract, topic

    def _decode(self, event, budget):
        budget.check()
        value = event.payload
        if isinstance(value, str):
            if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", value):
                value = bytes.fromhex(value)
            else:
                try:
                    value = base64.b64decode(value, validate=True)
                except ValueError:
                    return value
        if isinstance(value, bytes):
            value = self.contract._driver(budget).decode_event(self.topic, value)
        budget.check()
        return value

    def once(self, *, timeout=..., cancel=None):
        network = self.contract._require_network()
        with network.budget(timeout, cancel) as budget:
            with network.contracts.with_id(self.contract.id).subscribe(self.topic, _budget=budget) as subscription:
                return self._decode(subscription.get(_budget=budget), budget)

    def subscribe(self, **options):
        network = self.contract._require_network()
        return DecodedSubscription(self, network.contracts.with_id(self.contract.id).subscribe(self.topic, **options))

    def on(self, callback, **options):
        subscription = self.subscribe(**options)
        def handler(event):
            network = self.contract._require_network()
            with network.budget(cancel=subscription._token) as budget:
                result = self._decode(event, budget)
                if not subscription.closed:
                    callback(result)
        subscription._subscription.on(handler)
        return subscription


class DecodedSubscription:
    def __init__(self, events, subscription):
        from .cancellation import CancellationToken
        self._events, self._subscription = events, subscription
        self._token = CancellationToken()
        self._stop = subscription._lifetime.add_callback(
            lambda: self._token.cancel(Budget._reason(subscription._lifetime)))

    @property
    def closed(self):
        return self._subscription.closed

    def get(self, *, timeout=..., cancel=None):
        network = self._events.contract._require_network()
        with network.budget(timeout, cancel) as budget:
            remove = self._token.add_callback(lambda: budget.token.cancel(Budget._reason(self._token)))
            try:
                return self._events._decode(self._subscription.get(_budget=budget), budget)
            finally:
                remove()

    def close(self):
        self._token.cancel("subscription stopped")
        self._stop()
        self._subscription.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        return self.get(timeout=0)


class Contract:
    def __init__(self, contract_id, *, driver=None, network=None, bookentry=None):
        self._id = contract_bytes(contract_id)
        self._driver_instance = driver
        self.network, self.bookentry = network, bookentry
        if driver is None and network is None:
            raise ValueError("a driver or network registry is required")

    @property
    def id(self):
        return self._id.hex()

    def _require_network(self):
        if self.network is None:
            raise ValueError("contract operation requires a network")
        return self.network

    def _driver(self, budget):
        budget.check()
        driver = self._driver_instance
        if driver is None:
            driver = self._require_network().data_drivers.get(self.id, _budget=budget)
        budget.check()
        return driver

    def _budget(self, timeout, cancel):
        return Budget((self.network.timeout if self.network else 120) if timeout is ... else timeout, cancel)

    def schema(self, *, timeout=..., cancel=None):
        with self._budget(timeout, cancel) as budget:
            value = self._driver(budget).get_schema()
            budget.check()
            return value

    def version(self, *, timeout=..., cancel=None):
        with self._budget(timeout, cancel) as budget:
            value = self._driver(budget).get_version()
            budget.check()
            return value

    def encode(self, name, args=None, *, timeout=..., cancel=None, _budget=None):
        own = _budget is None
        budget = self._budget(timeout, cancel) if own else _budget
        try:
            value = self._driver(budget).encode(name, args)
            budget.check()
            return value
        finally:
            if own:
                budget.close()

    def call(self, name, args=None, *, feeder=False, timeout=..., cancel=None):
        network = self._require_network()
        with self._budget(timeout, cancel) as budget:
            encoded = self.encode(name, args, _budget=budget)
            raw = network.contracts.with_id(self.id).call(name, encoded, feeder=feeder, _budget=budget)
            result = self._driver(budget).decode_output_fn(name, raw)
            budget.check()
            return result

    def tx(self, name, args=None):
        if self.bookentry is None or self.bookentry.signer is None:
            raise ValueError("contract transaction requires a signer-backed BookEntry")
        self.bookentry.signer.assert_alive()
        encoded = self.encode(name, args, timeout=0)
        return self.bookentry.transfer(0).payload(ContractCall(self._id, name, encoded))

    def events(self, topic):
        return ContractEvents(self, topic)
