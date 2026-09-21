from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from sessionmcp.codeindex import connect as connect_code
from sessionmcp.indexer import connect as connect_index
from sessionmcp.vault import connect as connect_vault


class DatabasePermissionTests(unittest.TestCase):
    @staticmethod
    def _mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_transcript_index_and_sidecars_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "index.db"
            path.touch(mode=0o644)
            path.chmod(0o644)
            previous_umask = os.umask(0o022)
            try:
                conn = connect_index(path)
                conn.execute("BEGIN")
                artifacts = [
                    path,
                    path.with_name("index.db-wal"),
                    path.with_name("index.db-shm"),
                ]
                self.assertTrue(all(artifact.exists() for artifact in artifacts))
                self.assertEqual(
                    [0o600, 0o600, 0o600], [self._mode(p) for p in artifacts]
                )
            finally:
                os.umask(previous_umask)
                if "conn" in locals():
                    conn.close()

    def test_code_index_and_sidecars_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "code.db"
            previous_umask = os.umask(0o022)
            try:
                conn = connect_code(path)
                conn.execute("BEGIN")
                artifacts = [
                    path,
                    path.with_name("code.db-wal"),
                    path.with_name("code.db-shm"),
                ]
                self.assertTrue(all(artifact.exists() for artifact in artifacts))
                self.assertEqual(
                    [0o600, 0o600, 0o600], [self._mode(p) for p in artifacts]
                )
            finally:
                os.umask(previous_umask)
                if "conn" in locals():
                    conn.close()

    def test_vault_remains_private_without_wal_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "vault.db"
            path.touch(mode=0o644)
            path.chmod(0o644)

            conn = connect_vault(path)
            try:
                self.assertEqual(0o600, self._mode(path))
                self.assertFalse(path.with_name("vault.db-wal").exists())
                self.assertFalse(path.with_name("vault.db-shm").exists())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
