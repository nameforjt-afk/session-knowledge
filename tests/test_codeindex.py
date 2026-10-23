from __future__ import annotations

import argparse
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sessionmcp import codeindex, config
from sessionmcp.cli import cmd_code


class CodeIndexIntegrationTests(unittest.TestCase):
    def test_file_changed_during_read_is_retried_without_replacing_old_index(self) -> None:
        for replacement in (False, True):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "client.py"
                source.write_text("def old(): pass\n", encoding="utf-8")
                conn = codeindex.connect(root / "code.db")
                self.addCleanup(conn.close)
                writer = codeindex.CodeWriter(conn)
                original = codeindex.parse_file(source, str(root))
                assert original is not None
                writer.write(original)
                conn.commit()
                read_text = Path.read_text

                def change_after_read(path, *args, **kwargs):
                    text = read_text(path, *args, **kwargs)
                    if replacement:
                        other = root / "replacement.py"
                        other.write_text("def new(): pass\n", encoding="utf-8")
                        other.replace(source)
                    else:
                        source.write_text("def new(): pass\n", encoding="utf-8")
                    stamp = int(original.mtime) + 2
                    os.utime(source, (stamp, stamp))
                    return text

                with patch.object(Path, "read_text", change_after_read):
                    self.assertIsNone(codeindex.parse_file(source, str(root)))
                self.assertEqual("old", conn.execute("SELECT name FROM symbols").fetchone()[0])
                self.assertTrue(writer.needs_reindex(source))
                updated = codeindex.parse_file(source, str(root))
                assert updated is not None
                writer.write(updated)
                self.assertFalse(writer.needs_reindex(source))
                self.assertEqual("new", conn.execute("SELECT name FROM symbols").fetchone()[0])

    def test_python_symbols_and_imports_share_one_parse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "client.py"
            source.write_text(
                "import os.path\nfrom requests import get\nfrom .local import helper\n"
                "class Client:\n"
                "    async def send(self, value, *args, flag=True, **kwargs):\n"
                "        def nested(): pass\n"
                "        return value\n"
                "def outer(): pass\n",
                encoding="utf-8",
            )
            with patch.object(codeindex.ast, "parse", wraps=codeindex.ast.parse) as parse:
                parsed = codeindex.parse_file(source, temp)
            assert parsed is not None
            self.assertEqual(1, parse.call_count)
            self.assertEqual({"os", "requests"}, parsed.imports)
            self.assertEqual([
                ("Client", "class", 4, "", ""),
                ("send", "method", 5, "(self, value, *args, flag, **kwargs)", "Client"),
                ("outer", "function", 8, "()", ""),
                ("nested", "function", 6, "()", ""),
            ], parsed.symbols)
            source.write_text("def broken(\n", encoding="utf-8")
            broken = codeindex.parse_file(source, temp)
            assert broken is not None
            self.assertEqual([], broken.symbols)
            self.assertEqual(set(), broken.imports)

    def test_failed_parse_does_not_mark_full_rebuild_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            source = project / "client.py"
            source.write_text("import openai\n", encoding="utf-8")

            database = root / "code.db"
            old_conn = codeindex.connect(database)
            parsed = codeindex.parse_file(source, "project")
            self.assertIsNotNone(parsed)
            assert parsed is not None
            writer = codeindex.CodeWriter(old_conn)
            writer.write(parsed)
            old_conn.execute("UPDATE capabilities SET capability = 'stale'")
            old_conn.commit()
            old_conn.close()

            args = argparse.Namespace(code_action="index", force=False)
            with (
                patch.object(config, "CODE_DB", database),
                patch.object(config, "KNOWLEDGE_DIR", root / "knowledge"),
                patch.object(
                    config, "KNOWLEDGE_MAP", root / "knowledge" / "where.md"
                ),
                patch.object(
                    config,
                    "KNOWLEDGE_CANONICAL",
                    root / "knowledge" / "canonical.json",
                ),
                patch.object(
                    codeindex, "iter_code_files", return_value=[(source, "project")]
                ),
                patch.object(codeindex, "parse_file", return_value=None) as parse_mock,
            ):
                first_output = io.StringIO()
                with contextlib.redirect_stdout(first_output):
                    first_result = cmd_code(args)

                failed_conn = codeindex.connect(database)
                self.assertTrue(codeindex.CodeWriter(failed_conn).requires_full_reindex())
                failed_conn.close()

                parse_mock.side_effect = [parsed]
                second_output = io.StringIO()
                with contextlib.redirect_stdout(second_output):
                    second_result = cmd_code(args)

                completed_conn = codeindex.connect(database)
                groups = codeindex.find_implementation(
                    completed_conn, "", capability="openai"
                )["by_capability"]
                completed_conn.close()

            self.assertEqual(1, first_result)
            self.assertIn("解析失败 1", first_output.getvalue())
            self.assertEqual(0, second_result)
            self.assertIn("入库 1", second_output.getvalue())
            self.assertEqual(["openai"], [group["capability"] for group in groups])

    def test_missing_content_version_triggers_one_time_full_reindex(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            source = project / "client.py"
            source.write_text("import openai\n", encoding="utf-8")

            database = root / "code.db"
            old_conn = codeindex.connect(database)
            parsed = codeindex.parse_file(source, "project")
            self.assertIsNotNone(parsed)
            assert parsed is not None
            codeindex.CodeWriter(old_conn).write(parsed)
            old_conn.execute("UPDATE capabilities SET capability = 'stale'")
            old_conn.commit()
            old_conn.close()

            args = argparse.Namespace(code_action="index", force=False)
            with (
                patch.object(config, "CODE_DB", database),
                patch.object(config, "KNOWLEDGE_DIR", root / "knowledge"),
                patch.object(
                    config, "KNOWLEDGE_MAP", root / "knowledge" / "where.md"
                ),
                patch.object(
                    config,
                    "KNOWLEDGE_CANONICAL",
                    root / "knowledge" / "canonical.json",
                ),
                patch.object(
                    codeindex, "iter_code_files", return_value=[(source, "project")]
                ),
            ):
                first_output = io.StringIO()
                with contextlib.redirect_stdout(first_output):
                    cmd_code(args)

                migrated_conn = codeindex.connect(database)
                result = codeindex.find_implementation(
                    migrated_conn, "", capability="openai"
                )
                migrated_conn.close()

                second_output = io.StringIO()
                with contextlib.redirect_stdout(second_output):
                    cmd_code(args)

            self.assertEqual(
                ["openai"],
                [group["capability"] for group in result["by_capability"]],
            )
            self.assertIn("入库 1", first_output.getvalue())
            self.assertIn("跳过 1", second_output.getvalue())


if __name__ == "__main__":
    unittest.main()
