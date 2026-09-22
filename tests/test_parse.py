from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sessionmcp.parse import (
    KIND_ASSISTANT,
    KIND_COMPACT,
    KIND_TOOL_CALL,
    KIND_TOOL_ERROR,
    KIND_USER,
    parse_session,
)


class ParseSessionTests(unittest.TestCase):
    def _write_session(self, records: list[object]) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp_dir = tempfile.TemporaryDirectory()
        project_dir = Path(temp_dir.name) / "project-alpha"
        project_dir.mkdir()
        path = project_dir / "session-123.jsonl"
        path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
            encoding="utf-8",
        )
        return temp_dir, path

    def test_parses_user_assistant_and_compact_summary(self) -> None:
        temp_dir, path = self._write_session(
            [
                {"type": "custom-title", "customTitle": "Deploy notes"},
                {
                    "type": "user",
                    "timestamp": "2026-01-01T10:00:00Z",
                    "cwd": "/tmp/project-alpha",
                    "gitBranch": "main",
                    "message": {"content": "How should we deploy?"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-01-01T10:01:00Z",
                    "message": {"content": [{"type": "text", "text": "Use a staged rollout."}]},
                },
                {
                    "type": "user",
                    "timestamp": "2026-01-01T10:02:00Z",
                    "isCompactSummary": True,
                    "message": {"content": "This session is being continued from a previous conversation."},
                },
            ]
        )
        self.addCleanup(temp_dir.cleanup)

        parsed = parse_session(path)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual("Deploy notes", parsed.title)
        self.assertEqual("/tmp/project-alpha", parsed.cwd)
        self.assertEqual("main", parsed.git_branch)
        self.assertEqual(
            [KIND_USER, KIND_ASSISTANT, KIND_COMPACT],
            [chunk.kind for chunk in parsed.chunks],
        )
        self.assertEqual(1, parsed.compact_records)

    def test_tool_results_are_linked_and_secrets_are_redacted(self) -> None:
        temp_dir, path = self._write_session(
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-01T10:00:00Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Bash",
                                "input": {"command": "deploy --env staging"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-01-01T10:00:01Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call-1",
                                "is_error": True,
                                "content": "API_KEY=prod-secret-value-987654321",
                            }
                        ]
                    },
                },
            ]
        )
        self.addCleanup(temp_dir.cleanup)

        parsed = parse_session(path)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(1, len(parsed.tool_calls))
        self.assertTrue(parsed.tool_calls[0].is_error)
        self.assertNotIn("prod-secret-value-987654321", parsed.tool_calls[0].result_head)
        self.assertEqual([KIND_TOOL_CALL, KIND_TOOL_ERROR], [chunk.kind for chunk in parsed.chunks])

    def test_malformed_json_lines_are_ignored(self) -> None:
        temp_dir, path = self._write_session([])
        self.addCleanup(temp_dir.cleanup)
        path.write_text(
            'not-json\n{"type":"user","message":{"content":"valid instruction"}}\n',
            encoding="utf-8",
        )

        parsed = parse_session(path)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(["valid instruction"], [chunk.text for chunk in parsed.chunks])

    def test_markup_first_user_instruction_is_preserved(self) -> None:
        temp_dir, path = self._write_session(
            [
                {
                    "type": "user",
                    "message": {"content": "<div>Why is this layout broken?</div>"},
                }
            ]
        )
        self.addCleanup(temp_dir.cleanup)

        parsed = parse_session(path)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            ["<div>Why is this layout broken?</div>"],
            [chunk.text for chunk in parsed.chunks],
        )

    def test_known_system_markup_is_ignored(self) -> None:
        temp_dir, path = self._write_session(
            [
                {
                    "type": "user",
                    "message": {
                        "content": "<system-reminder>Internal context only.</system-reminder>"
                    },
                },
                {
                    "type": "user",
                    "message": {"content": "Keep this real instruction."},
                },
            ]
        )
        self.addCleanup(temp_dir.cleanup)

        parsed = parse_session(path)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            ["Keep this real instruction."],
            [chunk.text for chunk in parsed.chunks],
        )


if __name__ == "__main__":
    unittest.main()
