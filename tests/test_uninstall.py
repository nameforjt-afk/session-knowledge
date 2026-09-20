from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class UninstallIntegrationTests(unittest.TestCase):
    def test_uninstall_preserves_private_configs_and_unrelated_entries(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            claude_dir = home / ".claude"
            claude_dir.mkdir()
            claude_json = home / ".claude.json"
            settings = claude_dir / "settings.json"
            claude_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "session-knowledge": {"type": "stdio"},
                            "keep": {"type": "stdio"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": str(repo / "refresh.sh"),
                                        },
                                        {"type": "command", "command": "keep"},
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            claude_json.chmod(0o600)
            settings.chmod(0o600)
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'umask 022; exec bash "$1"',
                    "uninstall-test",
                    str(repo / "uninstall.sh"),
                ],
                cwd=repo,
                env=env,
                text=True,
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(0o600, stat.S_IMODE(claude_json.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(settings.stat().st_mode))
            self.assertEqual(
                {"keep"},
                set(json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"]),
            )
            hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"][
                "SessionStart"
            ][0]["hooks"]
            self.assertEqual(["keep"], [hook["command"] for hook in hooks])

    def test_uninstall_purge_removes_only_the_index_directory(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            data = home / ".claude" / "session-index"
            data.mkdir(parents=True)
            (data / "index.db").write_text("synthetic", encoding="utf-8")
            keep = home / ".claude" / "keep.txt"
            keep.write_text("keep", encoding="utf-8")
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = subprocess.run(
                ["bash", str(repo / "uninstall.sh"), "--purge"],
                cwd=repo,
                env=env,
                text=True,
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(data.exists())
            self.assertEqual("keep", keep.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
