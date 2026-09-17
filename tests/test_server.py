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

    def test_tool_schemas_publish_bounds_for_every_integer(self) -> None:
        response = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

        assert response is not None
        integer_fields: list[tuple[str, str, dict[str, object]]] = []
        for tool in response["result"]["tools"]:
            properties = tool["inputSchema"].get("properties", {})
            for name, schema in properties.items():
                if schema.get("type") == "integer":
                    integer_fields.append((tool["name"], name, schema))

        self.assertGreater(len(integer_fields), 0)
        for tool_name, field_name, schema in integer_fields:
            with self.subTest(tool=tool_name, field=field_name):
                self.assertIn("minimum", schema)
                self.assertIn("maximum", schema)

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

    def test_all_numeric_tool_arguments_are_validated_server_side(self) -> None:
        cases = [
            ("search_sessions", {"query": "deploy", "limit": 0}),
            ("get_session", {"session_id": "abc", "offset": -1}),
            ("list_sessions", {"limit": 101}),
            ("find_tool_call", {"pattern": "deploy", "limit": 0}),
            ("get_timeline", {"topic": "deploy", "limit": 101}),
            ("synthesize_topic", {"query": "deploy", "max_sessions": 21}),
            ("list_duplication", {"min_count": 1}),
        ]

        for request_id, (name, arguments) in enumerate(cases, 10):
            with self.subTest(tool=name):
                response = handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )

                assert response is not None
                self.assertEqual(-32602, response["error"]["code"])


if __name__ == "__main__":
    unittest.main()
