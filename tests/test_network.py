# SPDX-License-Identifier: MPL-2.0
import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.error import URLError

from w3sper import Client, GraphQLError, HTTPStatusError, NetworkError


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((self.path, self.headers, json.loads(body) if body else None))
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()
        self.wfile.write(self.server.body)


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.server.requests = []
        self.server.status = 200
        self.server.body = b'{"data":{"answer":42}}'
        self.client = Client(self.url)

    def respond(self, value):
        self.server.body = json.dumps(value).encode()

    def test_http_headers_path_and_variables(self):
        client = Client(self.url + "/prefix/?mode=test", version="custom")
        self.assertEqual(client.url, self.url)
        self.assertEqual(client.query(" answer ", variables={"number": 2**64}), {"answer": 42})
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/prefix/graphql?mode=test")
        self.assertEqual(headers["rusk-version"], "custom")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("application/graphql-response+json", headers["Accept"])
        self.assertEqual(body, {"query": "query { answer }", "variables": {"number": 2**64}})

    def test_graphql_document_detection(self):
        for document in ("query{answer}", "query A {answer}", "query($x:Int){answer}",
                         "mutation {answer}", "subscription {answer}",
                         "fragment A on B {answer}", "{answer}"):
            self.client.query(document)
            self.assertEqual(self.server.requests[-1][2]["query"], document)
        self.client.query("querying")
        self.assertEqual(self.server.requests[-1][2]["query"], "query { querying }")
        self.assertEqual(self.server.requests[-1][1]["rusk-version"], "1.0.0-rc.0")
        self.assertNotIn("variables", self.server.requests[-1][2])

    def test_typed_graphql_error_and_partial_data(self):
        errors = [{"message": "boom", "locations": [{"line": 1, "column": 2}]}]
        self.respond({"data": {"answer": None}, "errors": errors})
        for status in (200, 400):
            self.server.status = status
            with self.assertRaises(GraphQLError) as caught:
                self.client.query("answer")
            self.assertEqual(caught.exception.errors, errors)
            self.assertEqual(caught.exception.data, {"answer": None})
            self.assertEqual(str(caught.exception), "boom")

    def test_http_errors(self):
        for status, body in ((500, b"server error"), (404, b"{}"), (204, b""), (201, b'{"data":{}}')):
            self.server.status, self.server.body = status, body
            for method in (self.client.query, self.client.query_raw):
                with self.subTest(status=status), self.assertRaises(HTTPStatusError) as caught:
                    method("answer")
                self.assertEqual(caught.exception.status, status)

    def test_empty_and_malformed_responses(self):
        for body in (b"", b"  ", b"html!", b"\xff", b"[]", b"null", b"{}", b'{"data":null}',
                     b'{"data":[]}', b'{"errors":{}}', b'{"errors":[1]}', b'{"errors":[{}]}'):
            self.server.body = body
            with self.subTest(body=body), self.assertRaises(NetworkError):
                self.client.query("answer")
        self.respond({"data": {}, "errors": []})
        self.assertEqual(self.client.query("answer"), {})

    def test_query_raw_does_not_hide_graphql_body(self):
        self.server.body = b'{"errors":[{"message":"boom"}]}'
        self.assertEqual(self.client.query_raw("answer"), self.server.body)
        self.server.body = b" "
        with self.assertRaises(NetworkError):
            self.client.query_raw("answer")

    def test_exact_block_height(self):
        for height in (0, 2**53 + 1, 2**100, "9007199254740993"):
            self.respond({"data": {"block": {"header": {"height": height}}}})
            self.assertEqual(self.client.block_height(), int(height))
        self.assertEqual(self.server.requests[-1][2]["query"], "query { block(height: -1) { header { height } } }")

    def test_bad_block_height(self):
        for height in (-1, 1.5, True, None, "1.2", "1e9", "-1", "１", "", [], {}):
            self.respond({"data": {"block": {"header": {"height": height}}}})
            with self.assertRaises(NetworkError):
                self.client.block_height()
        self.respond({"data": {}})
        with self.assertRaises(NetworkError):
            self.client.block_height()

    def test_node_info(self):
        data = {"chain_id": 42, "version": "1.7.1", "extra": "retained"}
        self.respond(data)
        info = self.client.node_info()
        self.assertEqual((info.chain_id, info.data), (42, data))
        self.assertEqual(self.server.requests[-1][0], "/on/node/info")
        self.assertIsNone(self.server.requests[-1][2])
        self.assertEqual(info.chain, "unknown")
        snapshot = info.data
        snapshot["chain_id"] = 0
        self.assertEqual(info.data["chain_id"], 42)
        for chain_id in (None, -1, 256, True, 1.0, "42"):
            self.respond({"chain_id": chain_id})
            with self.assertRaises(NetworkError):
                self.client.node_info()
        self.respond({"data": {}})
        with self.assertRaises(NetworkError):
            self.client.node_info()

    def test_invalid_client_configuration(self):
        for url in ("", "ftp://example.com", "localhost:8080", "http:///path", "https://",
                    "https://host:65536", "https://host:bad", "http://user:secret@host", "https://host/#frag",
                    "http://ho st", "http://host\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Client(url)
        with self.assertRaises(TypeError):
            Client(None)
        self.assertEqual(Client(self.url, timeout=0).timeout, 0)
        for timeout in (-1, float("inf"), float("nan"), True, None):
            with self.assertRaises(ValueError):
                Client(self.url, timeout=timeout)
        for version in ("", "x\r\nInjected: 1", None, "é"):
            with self.assertRaises(ValueError):
                Client(self.url, version=version)

    def test_bad_request_never_touches_http(self):
        for query in ("", " ", None, 1):
            with self.assertRaises((TypeError, ValueError)):
                self.client.query(query)
        for variables in ([], "x", 1, {"x": float("nan")}):
            with self.assertRaises((TypeError, ValueError)):
                self.client.query("answer", variables=variables)
        self.assertEqual(self.server.requests, [])

    def test_timeout_and_transport_error_propagation(self):
        with patch("w3sper.network.request", side_effect=OSError("offline")) as opened:
            with self.assertRaises(OSError):
                Client(self.url, timeout=0.25).query("answer")
            self.assertEqual(opened.call_args.kwargs["timeout"], 0.25)

    def test_operation_name_and_request_object(self):
        self.client.query("query Answer {answer}", operation_name="Answer", variables={"value": 9})
        self.assertEqual(self.server.requests[-1][2]["operationName"], "Answer")
        self.client.query({"query": "{answer}", "variables": {}, "operationName": "Answer"})
        self.assertEqual(self.server.requests[-1][2]["variables"], {})
        with self.assertRaises(ValueError):
            self.client.query({"query": "{answer}"}, variables={})


if __name__ == "__main__":
    unittest.main()
