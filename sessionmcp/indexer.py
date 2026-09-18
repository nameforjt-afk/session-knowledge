"""构建与增量更新全文索引。

FTS5 的用法这里选了**独立表**而非 external content：

  chunks      存原文与元数据
  chunks_fts  只存 bigram 分词后的 token，chunk_id / session_id 作为 UNINDEXED 列

external content 表在增量删除时要求把原始内容原样回传给 'delete' 命令，一旦
分词逻辑变过就会留下孤儿索引项。独立表可以直接 DELETE ... WHERE session_id=?，
重建单个 session 干净利落。代价是 token 串多存一份——这正是预估里 bigram 展开
的那 2x。

原文只存一份（chunks.text），token 只存一份（chunks_fts.tok）。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import config
from .parse import ParsedSession
from .tokenize import tokenize

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    cwd             TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    git_branch      TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL DEFAULT '',
    ended_at        TEXT NOT NULL DEFAULT '',
    msg_count       INTEGER NOT NULL DEFAULT 0,
    tool_count      INTEGER NOT NULL DEFAULT 0,
    compact_count   INTEGER NOT NULL DEFAULT 0,
    compact_records INTEGER NOT NULL DEFAULT 0,
    user_records    INTEGER NOT NULL DEFAULT 0,
    is_private      INTEGER NOT NULL DEFAULT 0,
    is_subagent     INTEGER NOT NULL DEFAULT 0,
    parent_session  TEXT NOT NULL DEFAULT '',
    file_path       TEXT NOT NULL,
    file_mtime      REAL NOT NULL,
    file_size       INTEGER NOT NULL,
    indexed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_kind    ON chunks(kind);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    tok,
    chunk_id   UNINDEXED,
    session_id UNINDEXED,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    ts              TEXT NOT NULL DEFAULT '',
    tool_name       TEXT NOT NULL,
    target          TEXT NOT NULL DEFAULT '',
    params_redacted TEXT NOT NULL DEFAULT '',
    is_error        INTEGER NOT NULL DEFAULT 0,
    result_head     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_error   ON tool_calls(is_error);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开索引库，必要时建表。"""
    target = path or config.INDEX_DB
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class IndexWriter:
    """把解析结果写入索引库。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def needs_reindex(self, path: Path) -> bool:
        """文件自上次入库后是否变动过。

        活跃 session 每天都在追加，全量重建 800MB 不可接受，靠 mtime+size 判定。
        """
        row = self.conn.execute(
            "SELECT file_mtime, file_size FROM sessions WHERE session_id = ?",
            (path.stem,),
        ).fetchone()
        if row is None:
            return True
        stat = path.stat()
        return row["file_mtime"] != stat.st_mtime or row["file_size"] != stat.st_size

    def drop_session(self, session_id: str) -> None:
        """清掉一个 session 的全部索引数据，供重建使用。"""
        self.conn.execute("DELETE FROM chunks_fts WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM chunks WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def prune_missing(self, live_paths: set[Path]) -> int:
        """删除源文件已不存在的 session，返回删除数量。"""
        live = {str(path) for path in live_paths}
        stale = [
            row["session_id"]
            for row in self.conn.execute("SELECT session_id, file_path FROM sessions")
            if row["file_path"] not in live
        ]
        for session_id in stale:
            self.drop_session(session_id)
        return len(stale)

    def write(self, parsed: ParsedSession) -> None:
        """写入单个 session（先清后写，保证幂等）。"""
        self.drop_session(parsed.session_id)

        stat = Path(parsed.path).stat()
        self.conn.execute(
            """
            INSERT INTO sessions (
                session_id, project, cwd, title, git_branch,
                started_at, ended_at, msg_count, tool_count,
                compact_count, compact_records, user_records,
                is_private, is_subagent, parent_session,
                file_path, file_mtime, file_size, indexed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                parsed.session_id,
                parsed.project,
                parsed.cwd,
                parsed.title,
                parsed.git_branch,
                parsed.started_at,
                parsed.ended_at,
                parsed.msg_count,
                parsed.tool_count,
                parsed.compact_count,
                parsed.compact_records,
                parsed.user_records,
                1 if parsed.is_private else 0,
                1 if parsed.is_subagent else 0,
                parsed.parent_session_id,
                parsed.path,
                stat.st_mtime,
                stat.st_size,
                time.strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )

        # 私人 session 只留元数据，正文一概不入索引
        if parsed.is_private:
            return

        cursor = self.conn.cursor()
        for chunk in parsed.chunks:
            cursor.execute(
                "INSERT INTO chunks (session_id, seq, ts, kind, text) VALUES (?,?,?,?,?)",
                (parsed.session_id, chunk.seq, chunk.ts, chunk.kind, chunk.text),
            )
            cursor.execute(
                "INSERT INTO chunks_fts (tok, chunk_id, session_id) VALUES (?,?,?)",
                (tokenize(chunk.text), cursor.lastrowid, parsed.session_id),
            )

        cursor.executemany(
            """
            INSERT INTO tool_calls
                (session_id, seq, ts, tool_name, target, params_redacted, is_error, result_head)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    parsed.session_id,
                    call.seq,
                    call.ts,
                    call.tool_name,
                    call.target,
                    call.params,
                    1 if call.is_error else 0,
                    call.result_head,
                )
                for call in parsed.tool_calls
            ],
        )

    def optimize(self) -> None:
        """合并 FTS 索引段并回收空间。全量建库后跑一次。"""
        self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        self.conn.commit()
        self.conn.execute("VACUUM")
