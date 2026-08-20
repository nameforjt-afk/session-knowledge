"""把 Claude Code 的 session JSONL 解析成归一化记录。

关于 compact 摘要——这是整个解析里最容易出错的地方：

会话被自动压缩时，Claude 生成的摘要以一条 **普通 user 消息** 的形式写回文件，
正文开头是 "This session is being continued from a previous conversation…"，
靠 `isCompactSummary: true` 标记区分。而同样以尖括号开头或含有这句话的记录里，
还混着 8222 条真正的系统注入垃圾。

实测 175 个 session 里有 67 个被压缩过（38%），共 131 条摘要。对这 67 个 session
来说，摘要是被压缩掉的那部分对话**唯一幸存的记录**——原文已经永久丢失。所以必须
先判 `isCompactSummary` 标志再做前缀过滤，顺序反了就会把最值钱的东西当噪音丢掉。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import config
from .redact import Assignment, extract_assignments, redact

# chunk 的五种类型
KIND_USER = "user_instruction"
KIND_COMPACT = "compact_summary"
KIND_ASSISTANT = "assistant_text"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_ERROR = "tool_error"

_CONTINUED_MARKER = "being continued from a previous conversation"

# 单条工具结果里用于凭证抽取的最大扫描长度。工具结果总量 82M 字符，是全量数据的
# 大头，不设上限会被个别几十万字的输出拖住；取 200KB 足以覆盖任何 .env 或接口响应。
MAX_RESULT_SCAN = 200_000


@dataclass
class Chunk:
    seq: int
    ts: str
    kind: str
    text: str


@dataclass
class ToolCall:
    seq: int
    ts: str
    tool_name: str
    target: str
    params: str
    is_error: bool = False
    result_head: str = ""


@dataclass
class ParsedSession:
    session_id: str
    project: str
    path: str
    cwd: str = ""
    title: str = ""
    git_branch: str = ""
    started_at: str = ""
    ended_at: str = ""
    compact_count: int = 0
    # 摘要原始记录数。长摘要入库时会被切成多个 chunk，所以 chunk 计数对不上
    # 真实条数，自检必须比对这个字段。
    compact_records: int = 0
    user_records: int = 0
    is_private: bool = False
    # 子 agent 转录。内容价值真实存在（跑通的调用往往在子 agent 里），但它是主
    # 会话派生出来的执行细节，不是你自己的决策，检索时要排在主 session 之后。
    is_subagent: bool = False
    parent_session_id: str = ""
    chunks: list[Chunk] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)

    @property
    def msg_count(self) -> int:
        return len(self.chunks)

    @property
    def tool_count(self) -> int:
        return len(self.tool_calls)


# ---------------------------------------------------------------- 工具目标抽取

def _tool_target(tool_name: str, params: dict[str, Any]) -> str:
    """从工具入参里提炼出最有检索价值的那一项。

    Bash 取命令本身，文件类工具取路径，MCP 工具取入参摘要——这样
    find_tool_call("adspower update") 才能捞到东西。
    """
    if tool_name == "Bash":
        return str(params.get("command", ""))
    if tool_name in ("Read", "Write", "Edit", "NotebookEdit"):
        return str(params.get("file_path", params.get("notebook_path", "")))
    if tool_name in ("Grep", "Glob"):
        pattern = str(params.get("pattern", ""))
        path = str(params.get("path", ""))
        return f"{pattern} @ {path}" if path else pattern
    if tool_name in ("WebFetch", "WebSearch"):
        return str(params.get("url", params.get("query", "")))
    if tool_name in ("Agent", "Task"):
        return str(params.get("description", params.get("prompt", ""))[:200])
    if tool_name == "Skill":
        return str(params.get("skill", ""))

    # MCP 工具与其余：取所有标量入参拼成可读串
    parts = [
        f"{key}={value}"
        for key, value in params.items()
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 200
    ]
    return " ".join(parts)[:400]


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _split_long(text: str, limit: int = config.MAX_CHUNK_CHARS) -> list[str]:
    """超长正文按段落边界切分，尽量不在句中断开。"""
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"))
        if cut < limit // 2:
            cut = limit
        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        pieces.append(remaining)
    return pieces


# ---------------------------------------------------------------- 主解析

def parse_session(path: Path, *, parent_session_id: str = "") -> ParsedSession | None:
    """解析单个 session 文件。文件为空或完全不可读时返回 None。

    parent_session_id 非空表示这是一份子 agent 转录，其 project 要取父目录的父级
    （子文件躺在 <项目>/<父session>/subagents/ 下，直接取 parent.name 会拿到
    "subagents" 这个无意义的目录名）。
    """
    session_id = path.stem
    if parent_session_id:
        project = path.parent.parent.parent.name
    else:
        project = path.parent.name

    parsed = ParsedSession(
        session_id=session_id,
        project=project,
        path=str(path),
        is_subagent=bool(parent_session_id),
        parent_session_id=parent_session_id,
    )
    ai_title = ""
    custom_title = ""
    seq = 0
    pending: dict[str, ToolCall] = {}
    user_text_parts: list[str] = []

    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue

            record_type = record.get("type")

            # --- 会话级元数据 ---
            if record_type == "ai-title":
                ai_title = record.get("aiTitle") or ai_title
                continue
            if record_type == "custom-title":
                custom_title = record.get("customTitle") or custom_title
                continue

            timestamp = str(record.get("timestamp") or "")
            if timestamp:
                if not parsed.started_at or timestamp < parsed.started_at:
                    parsed.started_at = timestamp
                if not parsed.ended_at or timestamp > parsed.ended_at:
                    parsed.ended_at = timestamp

            if record.get("cwd"):
                parsed.cwd = str(record["cwd"])
            if record.get("gitBranch"):
                parsed.git_branch = str(record["gitBranch"])

            if record.get("subtype") == "compact_boundary":
                parsed.compact_count += 1
                continue

            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")

            # --- user 侧 ---
            if record_type == "user":
                if isinstance(content, str):
                    # 顺序不能反：先认摘要标志，再过滤噪音
                    if record.get("isCompactSummary"):
                        parsed.compact_records += 1
                        seq = _emit(parsed, seq, timestamp, KIND_COMPACT, content)
                        continue

                    stripped = content.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("<") or _CONTINUED_MARKER in stripped[:120]:
                        continue
                    if record.get("isMeta"):
                        continue

                    parsed.user_records += 1
                    user_text_parts.append(stripped[:2000])
                    seq = _emit(parsed, seq, timestamp, KIND_USER, stripped)

                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        call = pending.pop(str(block.get("tool_use_id", "")), None)
                        if call is None:
                            continue
                        body = _stringify(block.get("content"))

                        # 凭证抽取必须覆盖工具**结果**。`cat .env` / Read(.env) /
                        # 接口返回的 token 都落在这里——只扫入参会漏掉绝大部分：
                        # 实测 ADSPOWER_API_KEY 出现 256 次、DISCORD_TOKEN 188 次，
                        # 几乎全部来自结果而非入参。
                        parsed.assignments.extend(
                            extract_assignments(body[:MAX_RESULT_SCAN])
                        )

                        call.is_error = bool(block.get("is_error"))
                        if call.is_error:
                            # 报错全文保留——排错时需要完整信息
                            call.result_head = redact(body[: config.MAX_CHUNK_CHARS])[0]
                            seq = _emit(
                                parsed,
                                seq,
                                call.ts,
                                KIND_TOOL_ERROR,
                                f"{call.tool_name} 失败: {call.target}\n{body[:1500]}",
                            )
                        else:
                            call.result_head = redact(body[: config.TOOL_RESULT_HEAD])[0]

            # --- assistant 侧 ---
            elif record_type == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")

                    if block_type == "text":
                        text = str(block.get("text") or "").strip()
                        if text:
                            seq = _emit(parsed, seq, timestamp, KIND_ASSISTANT, text)

                    elif block_type == "tool_use":
                        tool_name = str(block.get("name") or "")
                        params = block.get("input")
                        params = params if isinstance(params, dict) else {}
                        target = _tool_target(tool_name, params)

                        raw_params = _stringify(params)
                        parsed.assignments.extend(extract_assignments(raw_params))

                        call = ToolCall(
                            seq=seq,
                            ts=timestamp,
                            tool_name=tool_name,
                            target=redact(target)[0],
                            params=redact(raw_params[: config.MAX_CHUNK_CHARS])[0],
                        )
                        parsed.tool_calls.append(call)
                        pending[str(block.get("id", ""))] = call

                        seq = _emit(
                            parsed,
                            seq,
                            timestamp,
                            KIND_TOOL_CALL,
                            f"{tool_name} {target}",
                            already_scanned=True,
                        )

    parsed.title = custom_title or ai_title
    parsed.is_private = config.looks_private(parsed.title, "\n".join(user_text_parts))

    if not parsed.chunks and not parsed.tool_calls:
        return None
    return parsed


def _emit(
    parsed: ParsedSession,
    seq: int,
    ts: str,
    kind: str,
    text: str,
    *,
    already_scanned: bool = False,
) -> int:
    """脱敏后写入 chunk，返回下一个序号。

    already_scanned 用于工具调用——同一段文本刚在上游抽过凭证，不必重复抽取，
    但仍然要脱敏。
    """
    if not already_scanned:
        parsed.assignments.extend(extract_assignments(text))

    clean, _ = redact(text)
    for piece in _split_long(clean):
        piece = piece.strip()
        if not piece:
            continue
        parsed.chunks.append(Chunk(seq=seq, ts=ts, kind=kind, text=piece))
        seq += 1
    return seq


def iter_session_files(projects_dir: Path | None = None) -> Iterator[Path]:
    """遍历所有顶层 session 文件（projects/<项目>/<uuid>.jsonl）。"""
    root = projects_dir or config.PROJECTS_DIR
    if not root.is_dir():
        return
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            if path.is_file() and os.path.getsize(path) > 0:
                yield path


def iter_subagent_files(projects_dir: Path | None = None) -> Iterator[tuple[Path, str]]:
    """遍历子 agent 转录（projects/<项目>/<父session>/subagents/agent-*.jsonl）。

    产出 (路径, 父 session id)。格式与主记录一致，同一个解析器就能处理。
    """
    root = projects_dir or config.PROJECTS_DIR
    if not root.is_dir():
        return
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        for parent_dir in sorted(project_dir.iterdir()):
            subagents = parent_dir / "subagents"
            if not parent_dir.is_dir() or not subagents.is_dir():
                continue
            for path in sorted(subagents.glob("*.jsonl")):
                if path.is_file() and os.path.getsize(path) > 0:
                    yield path, parent_dir.name
