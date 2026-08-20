"""凭证登记表。

跟全文索引完全分开的一个库，权限 0600，存真实值。分开的理由是硬性的：检索结果
会大量进入模型上下文，全文索引里不能有明文密钥；而凭证要能直接取用，就必须存原值。
两者放一个库里，任何一次宽泛检索都可能把密钥带出来。

索引里的 ⟦SECRET:指纹⟧ 就是这张表的外键——搜到令牌知道「这里有个凭证」，取值
要显式再查一次。

同名冲突不做静默选择。实测 47/89 个变量名存在多个不同取值（DATABASE_URL 29 个、
DISCORD_TOKEN 11 个、FEISHU_APP_ID 9 个），原因是多个飞书应用、多张多维表格、
开发与生产环境混在一起。自动挑一个必然出错，所以 get() 返回全部候选并附上证据，
由人判断。
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import config
from .parse import ParsedSession
from .redact import Assignment, is_local_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id             INTEGER PRIMARY KEY,
    key_name       TEXT NOT NULL,
    value          TEXT NOT NULL,
    value_fp       TEXT NOT NULL,
    service        TEXT NOT NULL DEFAULT 'other',
    kind           TEXT NOT NULL DEFAULT 'identifier',
    project        TEXT NOT NULL DEFAULT '',
    first_seen     TEXT NOT NULL DEFAULT '',
    last_seen      TEXT NOT NULL DEFAULT '',
    occurrences    INTEGER NOT NULL DEFAULT 0,
    is_placeholder INTEGER NOT NULL DEFAULT 0,
    is_local       INTEGER NOT NULL DEFAULT 0,
    source         TEXT NOT NULL DEFAULT 'session',
    source_path    TEXT NOT NULL DEFAULT '',
    source_session TEXT NOT NULL DEFAULT '',
    usage_example  TEXT NOT NULL DEFAULT '',
    UNIQUE(key_name, value_fp, project, source)
);

CREATE INDEX IF NOT EXISTS idx_cred_name    ON credentials(key_name);
CREATE INDEX IF NOT EXISTS idx_cred_fp      ON credentials(value_fp);
CREATE INDEX IF NOT EXISTS idx_cred_service ON credentials(service);
CREATE INDEX IF NOT EXISTS idx_cred_source  ON credentials(source);

-- 被判定为「不是真凭证」的指纹。索引每天都在重扫 session，光删记录没有用——
-- 下次刷新同样的值又会被抽取回来。必须记住「这个值不要」，否则 forget 只是
-- 把问题推迟两秒。
CREATE TABLE IF NOT EXISTS blocked (
    value_fp   TEXT PRIMARY KEY,
    key_name   TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    blocked_at TEXT NOT NULL DEFAULT ''
);
"""

# 来源可信度：.env 是当前真正在用的值，session 里的只是当时泄漏进对话的历史快照，
# 可能早已轮换。取值时按这个顺序排。
SOURCE_ENV = "env_file"
SOURCE_SESSION = "session"
_SOURCE_RANK = {SOURCE_ENV: 0, SOURCE_SESSION: 1}


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开凭证库并强制文件权限为 0600。

    journal_mode 保持默认的 DELETE 而非 WAL——WAL 会额外产生 -wal/-shm 两个
    文件，它们的权限不受这里的 chmod 保护，等于在旁边留了个明文副本。
    """
    target = path or config.VAULT_DB
    target.parent.mkdir(parents=True, exist_ok=True)

    existed = target.exists()
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()

    if not existed or (target.stat().st_mode & 0o077):
        os.chmod(target, 0o600)
    return conn


class VaultWriter:
    """把凭证观测累积进登记表。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._blocked = {
            row["value_fp"]
            for row in conn.execute("SELECT value_fp FROM blocked")
        }

    def _upsert(
        self,
        item: Assignment,
        *,
        project: str,
        stamp: str,
        source: str,
        source_path: str = "",
        session_id: str = "",
    ) -> None:
        if item.fingerprint in self._blocked:
            return
        self.conn.execute(
            """
            INSERT INTO credentials (
                key_name, value, value_fp, service, kind, project,
                first_seen, last_seen, occurrences,
                is_placeholder, is_local, source, source_path,
                source_session, usage_example
            ) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)
            ON CONFLICT(key_name, value_fp, project, source) DO UPDATE SET
                occurrences    = occurrences + 1,
                last_seen      = MAX(last_seen, excluded.last_seen),
                first_seen     = MIN(first_seen, excluded.first_seen),
                -- 同一个值可能同时出现在 .env 与 .env.example 里。只要有任何一个
                -- 来源认定它是真值，就按真值记；来源路径也跟着指向那个非模板文件，
                -- 否则先后写入顺序会决定显示哪条路径，纯属随机。
                is_placeholder = MIN(is_placeholder, excluded.is_placeholder),
                source_path    = CASE
                    WHEN source_path = '' OR excluded.is_placeholder < is_placeholder
                    THEN excluded.source_path ELSE source_path END,
                source_session = excluded.source_session,
                usage_example  = CASE
                    WHEN length(excluded.usage_example) > length(usage_example)
                    THEN excluded.usage_example ELSE usage_example END
            """,
            (
                item.key_name,
                item.value,
                item.fingerprint,
                item.service,
                item.kind,
                project,
                stamp,
                stamp,
                1 if item.is_placeholder else 0,
                1 if is_local_url(item.value) else 0,
                source,
                source_path,
                session_id,
                item.context,
            ),
        )

    def write(self, parsed: ParsedSession) -> int:
        """登记一个 session 里抽到的全部凭证，返回观测条数。

        私人 session 的凭证同样跳过——那类会话里出现的多半是个人账户信息。
        """
        if parsed.is_private or not parsed.assignments:
            return 0

        stamp = parsed.ended_at or parsed.started_at
        for item in parsed.assignments:
            self._upsert(
                item,
                project=parsed.project,
                stamp=stamp,
                source=SOURCE_SESSION,
                session_id=parsed.session_id,
            )
        return len(parsed.assignments)

    def write_env(
        self, assignments: list[Assignment], *, project: str, source_path: str, stamp: str
    ) -> int:
        """登记一个 .env 文件里的凭证。"""
        for item in assignments:
            self._upsert(
                item,
                project=project,
                stamp=stamp,
                source=SOURCE_ENV,
                source_path=source_path,
            )
        return len(assignments)

    def drop_env_rows(self) -> None:
        """清掉全部 .env 来源的记录，供重新扫描使用。

        .env 会被改写，旧值留着只会制造假候选，所以每次扫描前整体重来。
        """
        self.conn.execute("DELETE FROM credentials WHERE source = ?", (SOURCE_ENV,))


# ---------------------------------------------------------------- 查询

def _mask(value: str) -> str:
    """给出足以辨认、不足以使用的遮蔽形式。"""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}（{len(value)} 字符）"


def list_credentials(
    conn: sqlite3.Connection,
    service: str | None = None,
    key_name: str | None = None,
    include_placeholders: bool = False,
) -> list[dict[str, Any]]:
    """列出凭证名录。**永不返回真实值**，只给遮蔽形式与元数据。"""
    clauses: list[str] = []
    params: list[Any] = []

    if service:
        clauses.append("service = ?")
        params.append(service)
    if key_name:
        clauses.append("key_name LIKE ?")
        params.append(f"%{key_name}%")
    if not include_placeholders:
        clauses.append("is_placeholder = 0")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT key_name, service, kind,
               COUNT(*)                       AS variants,
               SUM(occurrences)               AS total_hits,
               MAX(last_seen)                 AS last_seen,
               GROUP_CONCAT(DISTINCT project) AS projects,
               MAX(source = 'env_file')       AS from_env
        FROM credentials
        {where}
        GROUP BY key_name, service, kind
        ORDER BY from_env DESC, (kind = 'secret') DESC, total_hits DESC, variants DESC
        """,
        params,
    ).fetchall()

    return [
        {
            "key_name": row["key_name"],
            "service": row["service"],
            "kind": row["kind"],
            "variants": row["variants"],
            "total_hits": row["total_hits"],
            "last_seen": (row["last_seen"] or "")[:10],
            "projects": (row["projects"] or "").split(","),
            "has_conflict": row["variants"] > 1,
            "in_env_file": bool(row["from_env"]),
        }
        for row in rows
    ]


def get_credential(
    conn: sqlite3.Connection,
    key_name: str,
    project: str | None = None,
    reveal: bool = True,
) -> list[dict[str, Any]]:
    """取某个变量的全部候选值，按可信度排序。

    排序依据：非占位符 > .env 现值优先于 session 历史快照 > 非本地地址 >
    项目匹配 > 最近出现 > 出现次数。

    绝不只返回第一条——同名多值是常态（多个飞书应用、多张表、开发与生产环境），
    静默挑一个就是在制造事故。
    """
    rows = conn.execute(
        "SELECT * FROM credentials WHERE key_name = ? COLLATE NOCASE",
        (key_name,),
    ).fetchall()

    def rank(row: sqlite3.Row) -> tuple[int, int, int, int, str, int]:
        project_match = 0 if (project and project in row["project"]) else 1
        return (
            row["is_placeholder"],
            _SOURCE_RANK.get(row["source"], 9),
            row["is_local"],
            project_match,
            # last_seen 需要倒序；字符串没法取负，改用逐字符取反的比较键
            "".join(chr(255 - ord(c)) if ord(c) < 255 else c for c in (row["last_seen"] or "")),
            -row["occurrences"],
        )

    ordered = sorted(rows, key=rank)

    return [
        {
            "key_name": row["key_name"],
            "value": row["value"] if reveal else None,
            "masked": _mask(row["value"]),
            "fingerprint": row["value_fp"],
            "service": row["service"],
            "kind": row["kind"],
            "project": row["project"],
            "source": row["source"],
            "source_path": row["source_path"],
            "first_seen": (row["first_seen"] or "")[:10],
            "last_seen": (row["last_seen"] or "")[:10],
            "occurrences": row["occurrences"],
            "is_placeholder": bool(row["is_placeholder"]),
            "is_local": bool(row["is_local"]),
            "source_session": row["source_session"],
            "usage_example": row["usage_example"],
        }
        for row in ordered
    ]


def forget(
    conn: sqlite3.Connection,
    *,
    fingerprint: str = "",
    key_name: str = "",
    reason: str = "",
) -> int:
    """删除误收的凭证并永久拉黑其指纹，返回删除条数。

    必须有这个能力。session 里出现的「凭证」不全是真的——文档示例、报错信息里的
    片段、讨论时随手编的样例值，都会被抽取进来，而且长得跟真值一模一样。一个混进
    候选列表的假值比没有这条记录更危险：它看起来完全合理，你会拿去用。

    删除同时写入 blocked 表。只删记录是没用的：索引每天自动重扫，两秒后同样的值
    又回来了。
    """
    if fingerprint:
        targets = [fingerprint]
        cursor = conn.execute("DELETE FROM credentials WHERE value_fp = ?", (fingerprint,))
    elif key_name:
        targets = [
            row["value_fp"]
            for row in conn.execute(
                "SELECT DISTINCT value_fp FROM credentials WHERE key_name = ? COLLATE NOCASE",
                (key_name,),
            )
        ]
        cursor = conn.execute(
            "DELETE FROM credentials WHERE key_name = ? COLLATE NOCASE", (key_name,)
        )
    else:
        return 0

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.executemany(
        """
        INSERT INTO blocked (value_fp, key_name, reason, blocked_at) VALUES (?,?,?,?)
        ON CONFLICT(value_fp) DO NOTHING
        """,
        [(fp, key_name, reason, stamp) for fp in targets],
    )
    conn.commit()
    return cursor.rowcount


def list_blocked(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """列出被拉黑的指纹。"""
    return [dict(row) for row in conn.execute("SELECT * FROM blocked ORDER BY blocked_at DESC")]


def unblock(conn: sqlite3.Connection, fingerprint: str) -> int:
    """撤销拉黑。下次 index 时该值会重新进入登记表。"""
    cursor = conn.execute("DELETE FROM blocked WHERE value_fp = ?", (fingerprint,))
    conn.commit()
    return cursor.rowcount


def lookup_fingerprint(conn: sqlite3.Connection, value_fp: str) -> list[dict[str, Any]]:
    """按指纹反查——用于把索引里的 ⟦SECRET:指纹⟧ 还原成具体是哪个凭证。"""
    rows = conn.execute(
        """
        SELECT key_name, service, kind, project, last_seen, occurrences, usage_example
        FROM credentials WHERE value_fp = ?
        """,
        (value_fp,),
    ).fetchall()
    return [dict(row) for row in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*)                          AS rows,
               COUNT(DISTINCT key_name)          AS names,
               SUM(is_placeholder)               AS placeholders,
               SUM(CASE WHEN kind='secret' THEN 1 ELSE 0 END) AS secrets
        FROM credentials
        """
    ).fetchone()
    conflicts = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT key_name FROM credentials WHERE is_placeholder = 0
            GROUP BY key_name HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    return {
        "rows": row["rows"] or 0,
        "distinct_names": row["names"] or 0,
        "placeholders": row["placeholders"] or 0,
        "secrets": row["secrets"] or 0,
        "conflicting_names": conflicts["n"] or 0,
    }
