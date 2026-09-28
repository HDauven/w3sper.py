# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""Dusk HTTP/GraphQL, RUES, proving and transaction execution client."""
import copy
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ._validation import integer, octets, uint
from .cancellation import Budget, validate_timeout
from .transport import HTTPStatusError, NetworkError, request
from .values import validate_chain_id
from .payload import STAKE, TRANSFER
from .rues import Rues, Target

DEFAULT_VERSION = "1.0.0-rc.0"


class GraphQLError(NetworkError):
    def __init__(self, errors, data=None):
        self.errors = copy.deepcopy(errors)
        self.data = copy.deepcopy(data)
        super().__init__(errors[0].get("message", "GraphQL error"))


@dataclass(frozen=True)
class NodeInfo:
    chain_id: int
    _data: dict

    @property
    def data(self):
        return copy.deepcopy(self._data)

    @property
    def chain(self):
        return {0: "localnet", 1: "mainnet", 2: "testnet", 3: "devnet"}.get(self.chain_id, "unknown")


class Node(Target):
    def info(self, **options):
        return self.network.node_info(**options)

    def crs(self, **options):
        return self.call("crs", **options)

    def provisioners(self, **options):
        return self.network._json(self.call("provisioners", **options))


class Blocks(Target):
    def gas_price(self, **options):
        data = self.network._json(self.call("gas-price", **options))
        if not isinstance(data, dict):
            raise NetworkError("invalid gas price response")
        return {name: uint(value, 64, "gas price") for name, value in data.items()}


class Contracts(Target):
    @property
    def transfer_contract(self):
        return self.with_id(TRANSFER)

    @property
    def stake_contract(self):
        return self.with_id(STAKE)


class Transactions(Target):
    def preverify(self, transaction, **options):
        data = transaction.buffer if hasattr(transaction, "buffer") else transaction
        self.call("preverify", data, **options)
        return transaction

    def propagate(self, transaction, **options):
        data = transaction.buffer if hasattr(transaction, "buffer") else transaction
        self.call("propagate", data, **options)
        return transaction


class Client:
    """Synchronous client with shared deadlines and explicit cancellation tokens.

    HTTP is independent of the WebSocket lifecycle. connect() is required only
    for events and WebSocket probes. close() also releases an internally created
    WalletCore and registered data drivers. No automatic nonce reservation/retry.
    """
    LOCALNET, MAINNET, TESTNET, DEVNET = 0, 1, 2, 3

    def __init__(self, node_url, *, version=DEFAULT_VERSION, timeout=120, core=None):
        if not isinstance(node_url, str):
            raise TypeError("node URL must be text")
        if any(c.isspace() or ord(c) < 32 for c in node_url):
            raise ValueError("invalid node URL")
        parsed = urlsplit(node_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise ValueError("an http(s) node URL without credentials or fragment is required")
        parsed.port
        if not isinstance(version, str) or not version or any(ord(c) < 32 or ord(c) > 126 for c in version):
            raise ValueError("invalid rusk-version header")
        self.timeout = validate_timeout(timeout)
        self.url = parsed._replace(path="", query="").geturl()
        self._endpoint = parsed._replace(path=parsed.path.rstrip("/") + "/graphql").geturl()
        self.version = version
        self._core, self._owns_core = core, core is None
        self._closed = False
        self.rues = Rues(self)
        self.node = Node(self, "node")
        self.blocks = Blocks(self, "blocks")
        self.contracts = Contracts(self, "contracts")
        self.transactions = Transactions(self, "transactions")
        from .data_driver import DataDriverRegistry
        self.data_drivers = DataDriverRegistry(timeout=self.timeout)

    @property
    def core(self):
        if self._closed:
            raise NetworkError("client is closed")
        if self._core is None:
            from .walletcore import WalletCore
            self._core = WalletCore()
        return self._core

    def budget(self, timeout=..., cancel=None):
        if self._closed:
            raise NetworkError("client is closed")
        return Budget(self.timeout if timeout is ... else timeout, cancel)

    @staticmethod
    def _json(body):
        try:
            return json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise NetworkError("invalid JSON response") from exc

    def _http(self, url, *, method="POST", body=b"", headers=None, timeout=..., cancel=None, _budget=None):
        if self._closed:
            raise NetworkError("client is closed")
        return request(url, method=method, body=body,
                       headers={"rusk-version": self.version, **(headers or {})},
                       timeout=self.timeout if timeout is ... else timeout, cancel=cancel, budget=_budget)

    def call(self, scope, topic, body=b"", *, identifier=None, feeder=False, stream=False,
             headers=None, timeout=..., cancel=None, _budget=None):
        path = Target(self, scope, identifier).path(topic)
        body = b"" if body is None else octets(body, "request body")
        headers = {"Content-Type": "application/octet-stream", **(headers or {})}
        if feeder:
            headers["Rusk-feeder"] = "true"
        response = self._http(self.url + path, body=body, headers=headers,
                              timeout=timeout, cancel=cancel, _budget=_budget)
        if not 200 <= response.status < 300:
            status, error = response.status, response.read()
            # A read-only contract feeder must be requested explicitly by the node.
            if not feeder and re.search(rb"Missing\s+feed", error, re.I):
                return self.call(scope, topic, body, identifier=identifier, feeder=True,
                                 stream=stream, headers=headers, timeout=timeout, cancel=cancel, _budget=_budget)
            raise HTTPStatusError(status, error)
        return response if stream else response.read()

    def _subscription_request(self, path, method, session, *, budget=None, timeout=...):
        with self._http(self.url + path, method=method, headers={"rusk-session-id": session},
                        _budget=budget, timeout=timeout) as response:
            body = response.read()
            if not 200 <= response.status < 300:
                raise HTTPStatusError(response.status, body)

    def _request(self, query, variables=None, *, operation_name=None, timeout=..., cancel=None, _budget=None):
        if isinstance(query, dict):
            if variables is not None or operation_name is not None:
                raise ValueError("do not combine request object with variables/operation_name")
            payload = copy.deepcopy(query)
            query = payload.get("query")
        else:
            payload = {}
        if not isinstance(query, str):
            raise TypeError("GraphQL query must be text")
        query = query.strip()
        if not query:
            raise ValueError("a GraphQL query is required")
        document = query.startswith("{") or re.match(r"(?:query|mutation|subscription|fragment)(?=$|[ \t\n\r{(])", query)
        payload["query"] = query if document else "query { " + query + " }"
        if variables is not None:
            payload["variables"] = variables
        if "variables" in payload and not isinstance(payload["variables"], dict):
            raise TypeError("GraphQL variables must be a dict")
        if operation_name is not None:
            payload["operationName"] = operation_name
        if "operationName" in payload and not isinstance(payload["operationName"], str):
            raise TypeError("operation_name must be text")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/graphql-response+json, application/json"}
        with self._http(self._endpoint, body=json.dumps(payload, allow_nan=False).encode(), headers=headers,
                        timeout=timeout, cancel=cancel, _budget=_budget) as response:
            return response.status, response.read()

    @staticmethod
    def _status(status, body):
        if status != 200:
            raise HTTPStatusError(status, body)
        if not body.strip():
            raise NetworkError("empty GraphQL response")

    def query_raw(self, query, *, variables=None, **options):
        status, body = self._request(query, variables, **options)
        self._status(status, body)
        return body

    def query(self, query, *, variables=None, **options):
        status, body = self._request(query, variables, **options)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            self._status(status, body)
            raise NetworkError("invalid GraphQL JSON response") from exc
        if not isinstance(payload, dict):
            self._status(status, body)
            raise NetworkError("GraphQL response must be an object")
        errors = payload.get("errors")
        if errors is not None:
            if not isinstance(errors, list) or any(not isinstance(e, dict) or not isinstance(e.get("message"), str) for e in errors):
                raise NetworkError("malformed GraphQL errors")
            if errors:
                raise GraphQLError(errors, payload.get("data"))
        self._status(status, body)
        if not isinstance(payload.get("data"), dict):
            raise NetworkError("GraphQL response is missing a data object")
        return payload["data"]

    def node_info(self, **options):
        data = self._json(self.node.call("info", **options))
        try:
            chain_id = validate_chain_id(data["chain_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkError("missing or invalid node chain id") from exc
        return NodeInfo(chain_id, copy.deepcopy(data))

    def block_height(self, **options):
        data = self.query("block(height: -1) { header { height } }", **options)
        try:
            height = data["block"]["header"]["height"]
            if isinstance(height, str) and re.fullmatch(r"[0-9]+", height):
                height = int(height)
            integer(height, "block height")
            if height < 0:
                raise ValueError("negative height")
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkError("missing or invalid block height") from exc
        return height

    def connect(self, **options):
        self.rues.connect(**options)
        return self

    @property
    def connected(self):
        return self.rues.connected

    def disconnect(self, **options):
        self.rues.disconnect(**options)

    def ping(self, **options):
        self.node_info(**options)

    def ping_websocket(self, **options):
        return self.rues.ping(**options)

    def execute(self, transaction, *, timeout=..., cancel=None):
        with self.budget(timeout, cancel) as budget:
            if hasattr(transaction, "build"):
                transaction = transaction.build(self, _budget=budget)
            self.transactions.preverify(transaction, _budget=budget)
            self.transactions.propagate(transaction, _budget=budget)
            return transaction

    def execute_and_wait(self, transaction, *, timeout=..., cancel=None):
        """Register execution interest before submitting, avoiding fast-block races."""
        with self.budget(timeout, cancel) as budget:
            if hasattr(transaction, "build"):
                transaction = transaction.build(self, _budget=budget)
            with self.transactions.with_id(transaction.hash).subscribe("executed", _budget=budget) as sub:
                self.transactions.preverify(transaction, _budget=budget)
                self.transactions.propagate(transaction, _budget=budget)
                return transaction, sub.get(_budget=budget)

    def prove(self, circuits, **options):
        return self.call("prover", "prove", circuits, **options)

    def close(self):
        if self._closed:
            return
        self.disconnect()
        self.data_drivers.close()
        if self._owns_core and self._core is not None:
            self._core.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


Network = Client
