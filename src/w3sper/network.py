# SPDX-License-Identifier: MPL-2.0
"""Small synchronous GraphQL-over-HTTP client. No RUES or live submission."""

import json
import math
import re
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ._validation import integer
from .values import validate_chain_id

DEFAULT_VERSION = "1.0.0-rc.0"


class NetworkError(RuntimeError):
    pass


class HTTPStatusError(NetworkError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"unexpected HTTP status {status}")


class GraphQLError(NetworkError):
    def __init__(self, errors: list[dict], data=None):
        self.errors = errors
        self.data = data
        super().__init__(errors[0].get("message", "GraphQL error"))


@dataclass(frozen=True)
class NodeInfo:
    chain_id: int
    data: dict


class Client:
    """POST JSON to <base path>/graphql using urllib and a finite timeout.

    urllib transport errors propagate; HTTP and GraphQL failures are distinct.
    HTTP is allowed for local nodes; prefer HTTPS for remote nodes.
    """

    def __init__(self, node_url: str, *, version: str = DEFAULT_VERSION, timeout: float = 30):
        if not isinstance(node_url, str):
            raise TypeError("node URL must be text")
        if any(c.isspace() or ord(c) < 32 for c in node_url):
            raise ValueError("invalid node URL")
        parsed = urlsplit(node_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.fragment):
            raise ValueError("an http(s) node URL without credentials or fragment is required")
        parsed.port  # Validate malformed/out-of-range port numbers before making requests.
        if (not isinstance(version, str) or not version
                or any(ord(c) < 32 or ord(c) > 126 for c in version)):
            raise ValueError("invalid rusk-version header")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be positive and finite")
        self.url = parsed._replace(path="", query="").geturl()
        self._endpoint = parsed._replace(path=parsed.path.rstrip("/") + "/graphql").geturl()
        self.version = version
        self.timeout = timeout

    def _request(self, query: str, variables: dict | None) -> tuple[int, bytes]:
        if not isinstance(query, str):
            raise TypeError("GraphQL query must be text")
        query = query.strip()
        if not query:
            raise ValueError("a GraphQL query is required")
        document = query.startswith("{") or re.match(
            r"(?:query|mutation|subscription|fragment)(?=$|[ \t\n\r{(])", query
        )
        if not document:
            query = "query { " + query + " }"
        payload = {"query": query}
        if variables is not None:
            if not isinstance(variables, dict):
                raise TypeError("GraphQL variables must be a dict")
            payload["variables"] = variables
        request = Request(
            self._endpoint, data=json.dumps(payload, allow_nan=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "application/graphql-response+json, application/json",
                     "rusk-version": self.version}, method="POST",
        )
        try:
            response = urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    @staticmethod
    def _status(status: int, body: bytes):
        if status != 200:
            raise HTTPStatusError(status)
        if not body.strip():
            raise NetworkError("empty GraphQL response")

    def query_raw(self, query: str, *, variables: dict | None = None) -> bytes:
        """Check HTTP status/nonempty body only; do not interpret GraphQL errors."""
        status, body = self._request(query, variables)
        self._status(status, body)
        return body

    def query(self, query: str, *, variables: dict | None = None) -> dict:
        status, body = self._request(query, variables)
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
            if not isinstance(errors, list) or any(
                not isinstance(e, dict) or not isinstance(e.get("message"), str) for e in errors
            ):
                raise NetworkError("malformed GraphQL errors")
            if errors:
                raise GraphQLError(errors, payload.get("data"))
        self._status(status, body)
        if not isinstance(payload.get("data"), dict):
            raise NetworkError("GraphQL response is missing a data object")
        return payload["data"]

    def node_info(self) -> NodeInfo:
        data = self.query("chain() { header { chainId } }")
        try:
            chain_id = validate_chain_id(data["chain"]["header"]["chainId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkError("missing or invalid node chain id") from exc
        return NodeInfo(chain_id, data)

    def block_height(self) -> int:
        data = self.query("block(height: -1) { header { height } }")
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
