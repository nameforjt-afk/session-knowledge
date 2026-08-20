"""检索核心。CLI 与 MCP server 共用这一份，避免两套逻辑漂移。

所有返回值都是原生 dict/list，方便直接序列化成 MCP 响应。

设计上的一条硬规矩：检索类接口一律返回**片段**，不返回整段原文。索引总量 800MB，
让模型自己去啃是不现实的，context 预算必须在这一层就控制住。要全文得显式调
get_session 分页读。
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .redact import REDACTION_PREFIX, scan_for_leaks
from .tokenize import build_query, snippet_around

# 检索结果里每条片段的长度
SNIPPET_WIDTH = 260

# 去重前多取几倍候选。
#
# Claude Code 恢复(resume)或分叉(fork)会话时会把整份记录复制成新文件，同一段对话
# 最多存在 14 份副本——实测 172 个 session 中 70 个属于这种副本，chunk 层面重复率
# 68.3%。不去重的话一次检索的结果会被同一段话的十几个副本占满。
DEDUPE_FETCH_FACTOR = 8
DEDUPE_FETCH_CAP = 400

# 子 agent 命中的降权系数。
#
# bm25() 返回负值、越小越靠前，所以乘一个 0~1 的系数会让分数向 0 靠拢、排到后面。
# 用乘法而不是加一个常数：bm25 的绝对量级随查询词频变化，加常数需要猜量级，乘法
# 天然按比例缩放。子 agent 的内容有真实价值（跑通的调用往往在里面），只是相对主
# 会话次要，所以是降权不是排除。
SUBAGENT_WEIGHT = 0.55

_BASE_COLUMNS = """
    c.chunk_id, c.session_id, c.seq, c.ts, c.kind, c.text,
    s.title, s.project, s.cwd, s.is_subagent, s.parent_session
"""


def _raw_terms(query: str) -> list[str]:
    """从查询串里取出用于高亮定位的原始词。"""
    return [term for term in query.split() if term]


def _filters(
    project: str | None,
    kind: str | None,
    since: str | None,
    until: str | None,
    include_private: bool,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if not include_private:
        clauses.append("s.is_private = 0")
    if project:
        clauses.append("s.project LIKE ?")
        params.append(f"%{project}%")
    if kind:
        clauses.append("c.kind = ?")
        params.append(kind)
    if since:
        clauses.append("c.ts >= ?")
        params.append(since)
    if until:
        clauses.append("c.ts <= ?")
        params.append(until)

    return clauses, params


def _row_to_hit(row: sqlite3.Row, terms: list[str], score: float | None) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "title": row["title"],
        "project": row["project"],
        "kind": row["kind"],
        "ts": row["ts"],
        "seq": row["seq"],
        "snippet": snippet_around(row["text"], terms, SNIPPET_WIDTH),
        "score": round(score, 3) if score is not None else None,
        "has_secret_ref": REDACTION_PREFIX in row["text"],
        "is_subagent": bool(row["is_subagent"]),
        "parent_session": row["parent_session"],
        "copies": 1,
    }


def _dedupe(hits: list[dict[str, Any]], texts: list[str], limit: int) -> list[dict[str, Any]]:
    """合并内容完全相同的命中，只保留分数最高的那条。

    副本数记在 copies 字段里——「这段话在 12 个 session 副本里都出现过」本身也是
    信息，比悄悄丢掉更诚实。
    """
    unique: dict[str, dict[str, Any]] = {}
    for hit, text in zip(hits, texts):
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        existing = unique.get(digest)
        if existing is None:
            unique[digest] = hit
        else:
            existing["copies"] += 1
            # 副本里保留最早的时间戳，它更接近这段话真正发生的时刻
            if hit["ts"] and (not existing["ts"] or hit["ts"] < existing["ts"]):
                existing["ts"] = hit["ts"]
    return list(unique.values())[:limit]


# ---------------------------------------------------------------- 第一档

def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    project: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    include_private: bool = False,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    """全文检索，返回片段与定位符。

    中文两字词经 bigram 分词后走 FTS5 索引；单字中文 bigram 表达不了，退回 LIKE
    过滤（这类查询很少，代价可接受）。

    默认合并完全相同的内容——会话副本会让同一段话重复十几次，不合并的话一页结果
    可能全是同一句。
    """
    parsed = build_query(query)
    if parsed.is_empty:
        return []

    terms = _raw_terms(query)
    clauses, params = _filters(project, kind, since, until, include_private)
    fetch = min(limit * DEDUPE_FETCH_FACTOR, DEDUPE_FETCH_CAP) if dedupe else limit

    if parsed.match:
        sql_params: list[Any] = [parsed.match, *params]
        for term in parsed.like_terms:
            clauses.append("c.text LIKE ?")
            sql_params.append(f"%{term}%")
        where = " AND ".join(["chunks_fts MATCH ?", *clauses])
        sql = f"""
            SELECT {_BASE_COLUMNS},
                   bm25(chunks_fts) * (CASE WHEN s.is_subagent THEN {SUBAGENT_WEIGHT} ELSE 1.0 END)
                       AS score
            FROM chunks_fts
            JOIN chunks   c ON c.chunk_id   = chunks_fts.chunk_id
            JOIN sessions s ON s.session_id = c.session_id
            WHERE {where}
            ORDER BY score
            LIMIT ?
        """
        sql_params.append(fetch)
    else:
        # 纯单字中文查询：没有可用的索引路径
        sql_params = list(params)
        for term in parsed.like_terms:
            clauses.append("c.text LIKE ?")
            sql_params.append(f"%{term}%")
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"""
            SELECT {_BASE_COLUMNS}, NULL AS score
            FROM chunks   c
            JOIN sessions s ON s.session_id = c.session_id
            WHERE {where}
            ORDER BY c.ts DESC
            LIMIT ?
        """
        sql_params.append(fetch)

    rows = conn.execute(sql, sql_params).fetchall()
    hits = [_row_to_hit(row, terms, row["score"]) for row in rows]
    if not dedupe:
        return hits[:limit]
    return _dedupe(hits, [row["text"] for row in rows], limit)


def get_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    offset: int = 0,
    limit: int = 50,
    kind: str | None = None,
) -> dict[str, Any]:
    """分页读取单个 session 的正文。"""
    meta = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ? OR session_id LIKE ?",
        (session_id, f"{session_id}%"),
    ).fetchone()
    if meta is None:
        return {"error": f"未找到 session: {session_id}"}

    clauses = ["session_id = ?"]
    params: list[Any] = [meta["session_id"]]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM chunks WHERE {' AND '.join(clauses)}", params
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        SELECT seq, ts, kind, text FROM chunks
        WHERE {' AND '.join(clauses)}
        ORDER BY seq LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return {
        "session_id": meta["session_id"],
        "title": meta["title"],
        "project": meta["project"],
        "cwd": meta["cwd"],
        "git_branch": meta["git_branch"],
        "started_at": meta["started_at"],
        "ended_at": meta["ended_at"],
        "was_compacted": bool(meta["compact_count"]),
        "compact_records": meta["compact_records"],
        "total_chunks": total,
        "offset": offset,
        "returned": len(rows),
        "chunks": [dict(row) for row in rows],
    }


def list_sessions(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    since: str | None = None,
    limit: int = 50,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    """列出 session 元数据。"""
    clauses: list[str] = []
    params: list[Any] = []

    if not include_private:
        clauses.append("is_private = 0")
    if project:
        clauses.append("project LIKE ?")
        params.append(f"%{project}%")
    if since:
        clauses.append("ended_at >= ?")
        params.append(since)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT session_id, title, project, git_branch, started_at, ended_at,
               msg_count, tool_count, compact_records, user_records
        FROM sessions {where}
        ORDER BY ended_at DESC LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    return [
        {
            "session_id": row["session_id"][:8],
            "title": row["title"] or "(无标题)",
            "project": row["project"],
            "branch": row["git_branch"],
            "date": (row["ended_at"] or "")[:10],
            "messages": row["msg_count"],
            "tools": row["tool_count"],
            "user_turns": row["user_records"],
            "was_compacted": bool(row["compact_records"]),
        }
        for row in rows
    ]


def find_tool_call(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    tool: str | None = None,
    session: str | None = None,
    errors_only: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """翻历史命令与 API 调用。

    24952 次 Bash 调用连同参数都在库里，「上次那个批量改指纹的命令怎么写的」
    这类问题走这个接口。
    """
    clauses = ["s.is_private = 0"]
    params: list[Any] = []

    for term in pattern.split():
        # 工具名也要参与匹配。MCP 工具的入参往往只是一串 id，真正有辨识度的是名字
        # 本身（mcp__adspower-local-api__update-browser），漏掉它就搜不到任何 MCP 调用。
        clauses.append("(t.tool_name LIKE ? OR t.target LIKE ? OR t.params_redacted LIKE ?)")
        params.extend([f"%{term}%"] * 3)
    if tool:
        clauses.append("t.tool_name LIKE ?")
        params.append(f"%{tool}%")
    if session:
        clauses.append("t.session_id LIKE ?")
        params.append(f"{session}%")
    if errors_only:
        clauses.append("t.is_error = 1")

    rows = conn.execute(
        f"""
        SELECT t.session_id, t.ts, t.tool_name, t.target, t.is_error,
               t.result_head, s.title, s.project
        FROM tool_calls t
        JOIN sessions   s ON s.session_id = t.session_id
        WHERE {' AND '.join(clauses)}
        ORDER BY t.ts DESC LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    return [
        {
            "session_id": row["session_id"][:8],
            "title": row["title"],
            "project": row["project"],
            "date": (row["ts"] or "")[:10],
            "tool": row["tool_name"],
            "target": row["target"][:400],
            "failed": bool(row["is_error"]),
            "result_head": row["result_head"][:300],
        }
        for row in rows
    ]


def get_timeline(
    conn: sqlite3.Connection,
    topic: str,
    *,
    since: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """按时间还原某个主题的推进过程，每个 session 取命中最强的一条。"""
    hits = search(conn, topic, since=since, limit=limit * 6)

    best: dict[str, dict[str, Any]] = {}
    for hit in hits:
        current = best.get(hit["session_id"])
        if current is None:
            best[hit["session_id"]] = dict(hit, hit_count=1)
        else:
            current["hit_count"] += 1

    ordered = sorted(best.values(), key=lambda item: item["ts"])
    return [
        {
            "date": (item["ts"] or "")[:10],
            "session_id": item["session_id"][:8],
            "title": item["title"],
            "project": item["project"],
            "hits": item["hit_count"],
            "snippet": item["snippet"],
        }
        for item in ordered[:limit]
    ]


# ---------------------------------------------------------------- 第二档

def synthesize_topic(
    conn: sqlite3.Connection,
    query: str,
    *,
    max_sessions: int = 8,
    per_session: int = 4,
    since: str | None = None,
) -> dict[str, Any]:
    """跨 session 取回压缩摘录包，交由模型侧归纳。

    「我在 DC 封号这件事上试过几种方案、结论分别是什么」这类问题单次检索答不了：
    答案分散在多个 session 里。这里按命中强度选出最相关的几个 session，每个只取
    几条最强摘录，把总量压到能进上下文的规模再交给模型。
    """
    hits = search(conn, query, since=since, limit=max_sessions * per_session * 5)
    if not hits:
        return {"query": query, "session_count": 0, "sessions": []}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        grouped.setdefault(hit["session_id"], []).append(hit)

    # 命中条数多的 session 优先——它更可能是这个主题的主战场
    ranked = sorted(grouped.items(), key=lambda item: -len(item[1]))[:max_sessions]

    bundles: list[dict[str, Any]] = []
    for session_id, session_hits in ranked:
        # 用户指令与 compact 摘要优先，它们承载决策；assistant 正文次之
        priority = {"user_instruction": 0, "compact_summary": 1, "tool_error": 2}
        chosen = sorted(
            session_hits,
            key=lambda h: (priority.get(h["kind"], 3), h["score"] if h["score"] is not None else 0),
        )[:per_session]

        bundles.append(
            {
                "session_id": session_id[:8],
                "title": chosen[0]["title"],
                "project": chosen[0]["project"],
                "date": (chosen[0]["ts"] or "")[:10],
                "total_hits": len(session_hits),
                "excerpts": [
                    {"kind": item["kind"], "text": item["snippet"]} for item in chosen
                ],
            }
        )

    bundles.sort(key=lambda item: item["date"])
    return {
        "query": query,
        "session_count": len(bundles),
        "total_hits": len(hits),
        "sessions": bundles,
    }


def track_evolution(
    conn: sqlite3.Connection,
    topic: str,
    *,
    since: str | None = None,
    max_points: int = 12,
) -> dict[str, Any]:
    """追踪一个主题从最早到最近的演进。

    「这个方案改了几版、每版为什么改」——按月分桶，每桶取最有代表性的一条，
    让时间线上的变化能直接读出来。
    """
    hits = search(conn, topic, since=since, limit=max_points * 15)
    if not hits:
        return {"topic": topic, "points": []}

    buckets: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        month = (hit["ts"] or "")[:7]
        if month:
            buckets.setdefault(month, []).append(hit)

    priority = {"user_instruction": 0, "compact_summary": 1}
    points: list[dict[str, Any]] = []
    for month in sorted(buckets):
        group = buckets[month]
        representative = sorted(
            group,
            key=lambda h: (priority.get(h["kind"], 2), h["score"] if h["score"] is not None else 0),
        )[0]
        points.append(
            {
                "month": month,
                "hits": len(group),
                "sessions": len({h["session_id"] for h in group}),
                "kind": representative["kind"],
                "title": representative["title"],
                "snippet": representative["snippet"],
            }
        )

    return {
        "topic": topic,
        "span": f"{points[0]['month']} → {points[-1]['month']}" if points else "",
        "points": points[-max_points:],
    }


# ---------------------------------------------------------------- 自检

def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """索引概况。数字须与建库前的实测统计吻合，对不上说明解析有问题。"""
    sessions = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(is_private)                                   AS private,
               SUM(CASE WHEN compact_records > 0 THEN 1 ELSE 0 END) AS compacted,
               SUM(compact_records)                              AS compact_records,
               SUM(user_records)                                 AS user_records,
               MIN(NULLIF(started_at,''))                        AS first_ts,
               MAX(NULLIF(ended_at,''))                          AS last_ts
        FROM sessions
        """
    ).fetchone()

    kinds = {
        row["kind"]: row["n"]
        for row in conn.execute("SELECT kind, COUNT(*) AS n FROM chunks GROUP BY kind")
    }
    tools = {
        row["tool_name"]: row["n"]
        for row in conn.execute(
            "SELECT tool_name, COUNT(*) AS n FROM tool_calls GROUP BY tool_name ORDER BY n DESC LIMIT 12"
        )
    }
    projects = [
        {"project": row["project"], "sessions": row["n"]}
        for row in conn.execute(
            "SELECT project, COUNT(*) AS n FROM sessions GROUP BY project ORDER BY n DESC"
        )
    ]

    total_chunks = sum(kinds.values())
    unique_chunks = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT text FROM chunks)"
    ).fetchone()["n"]

    return {
        "sessions": sessions["total"] or 0,
        "private_excluded": sessions["private"] or 0,
        "compacted_sessions": sessions["compacted"] or 0,
        "compact_records": sessions["compact_records"] or 0,
        "user_records": sessions["user_records"] or 0,
        "span": f"{(sessions['first_ts'] or '')[:10]} → {(sessions['last_ts'] or '')[:10]}",
        "chunks_by_kind": kinds,
        "total_chunks": total_chunks,
        "unique_chunks": unique_chunks,
        # 会话副本导致的重复率。检索默认已合并，这个数字只用于了解数据形态。
        "duplicate_ratio": round(1 - unique_chunks / total_chunks, 3) if total_chunks else 0.0,
        "tool_calls": tools,
        "projects": projects,
    }


def verify_redaction(conn: sqlite3.Connection, sample: int | None = None) -> dict[str, Any]:
    """扫描索引全库，确认没有明文密钥残留。命中数必须为 0。"""
    leaks: dict[str, int] = {}
    examples: list[dict[str, str]] = []
    scanned = 0

    limit = f"LIMIT {sample}" if sample else ""
    for row in conn.execute(f"SELECT chunk_id, session_id, text FROM chunks {limit}"):
        scanned += 1
        for label in scan_for_leaks(row["text"]):
            leaks[label] = leaks.get(label, 0) + 1
            if len(examples) < 5:
                examples.append({"session_id": row["session_id"][:8], "label": label})

    for row in conn.execute(f"SELECT session_id, target, params_redacted, result_head FROM tool_calls {limit}"):
        scanned += 1
        blob = f"{row['target']}\n{row['params_redacted']}\n{row['result_head']}"
        for label in scan_for_leaks(blob):
            leaks[label] = leaks.get(label, 0) + 1
            if len(examples) < 5:
                examples.append({"session_id": row["session_id"][:8], "label": label})

    return {
        "scanned_records": scanned,
        "leak_count": sum(leaks.values()),
        "leaks_by_type": leaks,
        "examples": examples,
        "clean": not leaks,
    }
