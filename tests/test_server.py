from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sessionmcp import server
from sessionmcp.indexer import connect
from sessionmcp.server import SERVER_NAME, handle


class ServerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.index_conn = connect(Path(self.temp_dir.name) / "index.db")
        self.addCleanup(self.index_conn.close)
        previous = server._index_conn
        server._index_conn = self.index_conn
        self.addCleanup(setattr, server, "_index_conn", previous)

    def test_initialize_negotiates_a_supported_protocol(self) -> None:
        response = handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )

        assert response is not None
        self.assertEqual("2025-06-18", response["result"]["protocolVersion"])
        self.assertEqual(SERVER_NAME, response["result"]["serverInfo"]["name"])

    def test_unknown_protocol_falls_back_to_default(self) -> None:
        response = handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "future-version"},
            }
        )

        assert response is not None
        self.assertEqual("2024-11-05", response["result"]["protocolVersion"])

    def test_tools_list_hides_internal_handlers(self) -> None:
        response = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

        assert response is not None
        tools = response["result"]["tools"]
        self.assertGreaterEqual(len(tools), 10)
        self.assertTrue(all("handler" not in tool for tool in tools))

    def test_unknown_method_returns_json_rpc_error(self) -> None:
        response = handle({"jsonrpc": "2.0", "id": 3, "method": "unknown"})

        assert response is not None
        self.assertEqual(-32601, response["error"]["code"])

    def test_notifications_do_not_produce_responses(self) -> None:
        response = handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

        self.assertIsNone(response)

    def test_negative_search_limit_is_rejected_as_invalid_params(self) -> None:
        response = handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_sessions",
                    "arguments": {"query": "deploy", "limit": -1},
                },
            }
        )

        assert response is not None
        self.assertEqual(-32602, response["error"]["code"])


if __name__ == "__main__":
    unittest.main()
