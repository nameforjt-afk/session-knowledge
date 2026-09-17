from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sessionmcp import query
from sessionmcp.indexer import IndexWriter, connect
from sessionmcp.parse import Chunk, ParsedSession


class IndexQueryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.conn = connect(root / "index.db")
        self.addCleanup(self.conn.close)
        self.writer = IndexWriter(self.conn)
        self.root = root

    def _write_session(
        self,
        session_id: str,
        text: str,
        *,
        project: str = "alpha",
        private: bool = False,
        subagent: bool = False,
    ) -> None:
        source = self.root / f"{session_id}.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        parsed = ParsedSession(
            session_id=session_id,
            project=project,
            path=str(source),
            title=f"Session {session_id}",
            started_at="2026-01-01T10:00:00Z",
            ended_at="2026-01-01T10:01:00Z",
            is_private=private,
            is_subagent=subagent,
            parent_session_id="parent-1" if subagent else "",
            chunks=[Chunk(0, "2026-01-01T10:00:00Z", "user_instruction", text)],
        )
        self.writer.write(parsed)
        self.conn.commit()

    def test_search_returns_matching_session_metadata(self) -> None:
        self._write_session("session-1", "部署流程需要灰度发布")

        hits = query.search(self.conn, "部署")

        self.assertEqual(1, len(hits))
        self.assertEqual("session-1", hits[0]["session_id"])
        self.assertEqual("alpha", hits[0]["project"])
        self.assertIn("部署", hits[0]["snippet"])

    def test_cjk_phrase_does_not_match_bigrams_scattered_across_text(self) -> None:
        self._write_session("exact", "部署流程需要灰度发布")
        self._write_session("scattered", "部署完成。署流异常。流程结束")

        hits = query.search(self.conn, "部署流程", dedupe=False)

        self.assertEqual(["exact"], [hit["session_id"] for hit in hits])

    def test_search_filters_by_project(self) -> None:
        self._write_session("session-1", "shared deployment note", project="alpha")
        self._write_session("session-2", "shared deployment note", project="beta")

        hits = query.search(self.conn, "deployment", project="beta")

        self.assertEqual(["beta"], [hit["project"] for hit in hits])

    def test_private_session_content_is_not_indexed(self) -> None:
        self._write_session("private-1", "medical private record", private=True)

        self.assertEqual([], query.search(self.conn, "medical"))
        self.assertEqual([], query.list_sessions(self.conn))

    def test_duplicate_content_is_collapsed(self) -> None:
        self._write_session("session-1", "same deployment guidance")
        self._write_session("session-2", "same deployment guidance")

        hits = query.search(self.conn, "deployment")

        self.assertEqual(1, len(hits))
        self.assertEqual(2, hits[0]["copies"])

    def test_reindex_replaces_old_content(self) -> None:
        self._write_session("session-1", "old deployment approach")
        self._write_session("session-1", "new migration approach")

        self.assertEqual([], query.search(self.conn, "deployment"))
        self.assertEqual(1, len(query.search(self.conn, "migration")))

    def test_get_session_paginates_chunks(self) -> None:
        source = self.root / "session-page.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        parsed = ParsedSession(
            session_id="session-page",
            project="alpha",
            path=str(source),
            title="Paged session",
            chunks=[
                Chunk(0, "2026-01-01T10:00:00Z", "user_instruction", "first"),
                Chunk(1, "2026-01-01T10:01:00Z", "assistant_text", "second"),
            ],
        )
        self.writer.write(parsed)
        self.conn.commit()

        page = query.get_session(self.conn, "session-page", offset=1, limit=1)

        self.assertEqual(2, page["total_chunks"])
        self.assertEqual(["second"], [chunk["text"] for chunk in page["chunks"]])

    def test_get_session_rejects_an_ambiguous_id_prefix(self) -> None:
        self._write_session("abcdef01-first", "first private decision")
        self._write_session("abcdef01-second", "second private decision")

        result = query.get_session(self.conn, "abcdef01")

        self.assertIn("error", result)
        self.assertEqual(
            ["abcdef01-first", "abcdef01-second"],
            result["matches"],
        )
        self.assertNotIn("chunks", result)


if __name__ == "__main__":
    unittest.main()
