from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


class RefreshIntegrationTests(unittest.TestCase):
    def test_slow_indexes_refresh_after_24_hours(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            data = home / ".claude" / "session-index"
            data.mkdir(parents=True)

            script = root / "refresh.sh"
            shutil.copy2(repo / "refresh.sh", script)

            calls = root / "python-calls"
            python = root / "record-python"
            python.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS_FILE"\n',
                encoding="utf-8",
            )
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            (root / ".python-path").write_text(str(python), encoding="utf-8")

            stamp = data / ".last-env-scan"
            stamp.touch()
            twenty_five_hours_ago = time.time() - (25 * 60 * 60)
            os.utime(stamp, (twenty_five_hours_ago, twenty_five_hours_ago))

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["CALLS_FILE"] = str(calls)
            result = subprocess.run(
                ["bash", str(script)],
                cwd=root,
                env=env,
                text=True,
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [
                    "-m sessionmcp.cli index",
                    "-m sessionmcp.cli scan-env",
                    "-m sessionmcp.cli code index",
                ],
                calls.read_text(encoding="utf-8").splitlines(),
            )


if __name__ == "__main__":
    unittest.main()
