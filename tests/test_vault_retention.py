from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sessionmcp.parse import ParsedSession
from sessionmcp.redact import extract_assignments
from sessionmcp.vault import VaultWriter, connect, get_credential


class VaultRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.conn = connect(self.root / "vault.db")
        self.addCleanup(self.conn.close)
        self.writer = VaultWriter(self.conn)
        self.writer.reset_session_observations()

    def _session(self, session_id: str, source: Path, value: str) -> ParsedSession:
        source.write_text("{}\n", encoding="utf-8")
        return ParsedSession(
            session_id=session_id,
            project="alpha",
            path=str(source),
            started_at="2026-01-01T10:00:00Z",
            ended_at="2026-01-01T10:01:00Z",
            assignments=extract_assignments(f"API_KEY={value}"),
        )

    def test_deleted_session_only_credential_disappears(self) -> None:
        source = self.root / "only.jsonl"
        self.writer.write(self._session("only", source, "prod-secret-value-123456"))
        self.writer.finish_session_refresh({source})
        self.assertEqual(1, len(get_credential(self.conn, "API_KEY")))

        source.unlink()
        pruned = self.writer.finish_session_refresh(set())

        self.assertEqual(1, pruned)
        self.assertEqual([], get_credential(self.conn, "API_KEY"))

    def test_shared_credential_remains_until_every_source_is_deleted(self) -> None:
        first = self.root / "first.jsonl"
        second = self.root / "second.jsonl"
        value = "shared-secret-value-123456"
        self.writer.write(self._session("first", first, value))
        self.writer.write(self._session("second", second, value))
        self.writer.finish_session_refresh({first, second})

        first.unlink()
        self.writer.finish_session_refresh({second})

        rows = get_credential(self.conn, "API_KEY")
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["occurrences"])
        self.assertEqual("second", rows[0]["source_session"])


if __name__ == "__main__":
    unittest.main()
