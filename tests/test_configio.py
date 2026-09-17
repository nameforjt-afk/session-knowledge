from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from sessionmcp.configio import atomic_write_json


class AtomicConfigWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    @staticmethod
    def _mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_existing_private_file_keeps_its_mode_and_backup(self) -> None:
        path = self.root / ".claude.json"
        path.write_text('{"existing": true}\n', encoding="utf-8")
        path.chmod(0o600)

        atomic_write_json(path, {"updated": True}, backup=True)

        self.assertEqual(0o600, self._mode(path))
        self.assertEqual({"updated": True}, json.loads(path.read_text(encoding="utf-8")))
        backup = self.root / ".claude.json.bak"
        self.assertEqual(0o600, self._mode(backup))
        self.assertEqual({"existing": True}, json.loads(backup.read_text(encoding="utf-8")))

    def test_new_config_file_is_private(self) -> None:
        path = self.root / "nested" / "settings.json"

        atomic_write_json(path, {"hooks": {}})

        self.assertEqual(0o600, self._mode(path))
        self.assertEqual({"hooks": {}}, json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
