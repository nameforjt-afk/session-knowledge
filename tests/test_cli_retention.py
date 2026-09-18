from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sessionmcp import config, query, vault
from sessionmcp.cli import cmd_index
from sessionmcp.indexer import connect as connect_index


class CliRetentionIntegrationTests(unittest.TestCase):
    def test_refresh_prunes_deleted_transcript_and_its_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            projects = root / "projects"
            project = projects / "alpha"
            project.mkdir(parents=True)
            transcript = project / "session-1.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-01-01T10:00:00Z",
                        "message": {
                            "content": "obsolete deployment API_KEY=prod-secret-value-123456"
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(force=False, no_subagents=False, no_optimize=True)

            with (
                patch.object(config, "PROJECTS_DIR", projects),
                patch.object(config, "INDEX_DB", root / "index.db"),
                patch.object(config, "VAULT_DB", root / "vault.db"),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                cmd_index(args)
                transcript.unlink()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    cmd_index(args)

                index_conn = connect_index(root / "index.db")
                self.addCleanup(index_conn.close)
                vault_conn = vault.connect(root / "vault.db")
                self.addCleanup(vault_conn.close)

                self.assertEqual([], query.search(index_conn, "obsolete"))
                self.assertEqual([], vault.get_credential(vault_conn, "API_KEY"))
                self.assertIn("清理已删除 session 1", output.getvalue())
                self.assertIn("清理凭证观测 1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
