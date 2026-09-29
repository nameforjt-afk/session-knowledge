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
    def _prepare_refresh(
        self, temp: str, *, fail_scan_env: bool = False
    ) -> tuple[Path, Path, dict[str, str]]:
        repo = Path(__file__).resolve().parents[1]
        root = Path(temp)
        home = root / "home"
        data = home / ".claude" / "session-index"
        data.mkdir(parents=True)

        script = root / "refresh.sh"
        shutil.copy2(repo / "refresh.sh", script)

        calls = root / "python-calls"
        python = root / "record-python"
        python.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$CALLS_FILE"\n'
            'if [ "${FAIL_SCAN_ENV:-}" = "1" ] && '
            '[ "$*" = "-m sessionmcp.cli scan-env" ]; then exit 1; fi\n',
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
        if fail_scan_env:
            env["FAIL_SCAN_ENV"] = "1"
        return script, calls, env

    def test_slow_indexes_refresh_after_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script, calls, env = self._prepare_refresh(temp)
            result = subprocess.run(
                ["bash", str(script)],
                cwd=Path(temp),
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

    def test_failed_slow_refresh_is_retried_on_the_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script, calls, env = self._prepare_refresh(temp, fail_scan_env=True)

            results = [
                subprocess.run(
                    ["bash", str(script)],
                    cwd=Path(temp),
                    env=env,
                    text=True,
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
                for _ in range(2)
            ]

            self.assertEqual([0, 0], [result.returncode for result in results])
            self.assertEqual(
                [
                    "-m sessionmcp.cli index",
                    "-m sessionmcp.cli scan-env",
                    "-m sessionmcp.cli code index",
                    "-m sessionmcp.cli index",
                    "-m sessionmcp.cli scan-env",
                    "-m sessionmcp.cli code index",
                ],
                calls.read_text(encoding="utf-8").splitlines(),
            )
            log = Path(env["HOME"]) / ".claude" / "session-index" / "refresh.log"
            self.assertIn("慢速索引刷新失败", log.read_text(encoding="utf-8"))

    def test_refresh_repairs_private_data_and_log_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script, _, env = self._prepare_refresh(temp)
            data = Path(env["HOME"]) / ".claude" / "session-index"
            data.chmod(0o755)

            result = subprocess.run(
                ["bash", str(script)],
                cwd=Path(temp),
                env=env,
                text=True,
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(0o700, stat.S_IMODE(data.stat().st_mode))
            self.assertEqual(
                0o600,
                stat.S_IMODE((data / "refresh.log").stat().st_mode),
            )


if __name__ == "__main__":
    unittest.main()
