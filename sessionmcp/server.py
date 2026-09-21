"""MCP stdio server。

这里手写 JSON-RPC 而不引 mcp SDK，是为了零依赖——系统 python 直接就能起，不会
因为虚拟环境、SDK 版本变动而在某天突然连不上。协议本身只有三个方法，不值得为它
背一个依赖。

stdout 只允许出现协议消息，任何日志都必须走 stderr，否则会污染协议流。

凭证的两个工具是刻意分开的：list_credentials 永不返回真实值，get_credential 才
返回。这样模型在探索阶段不会把密钥拖进上下文，只有明确需要时才取。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import __version__, config
from . import query as q
from . import vault as v
from .indexer import connect as connect_index
from .vault import connect as connect_vault

SERVER_NAME = "session-knowledge"
SERVER_VERSION = __version__
DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

_index_conn = None
_vault_conn = None
_code_conn = None


def _index():
    global _index_conn
    if _index_conn is None:
        _index_conn = connect_index()
    return _index_conn


def _vault():
    global _vault_conn
    if _vault_conn is None:
        _vault_conn = connect_vault()
    return _vault_conn


def _log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def _bounded_int(
    kwargs: dict[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Parse a bounded integer supplied by an MCP client."""
    raw = kwargs.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise TypeError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


# ---------------------------------------------------------------- 工具实现

def _search_sessions(**kwargs: Any) -> Any:
    limit = _bounded_int(kwargs, "limit", 15, minimum=1, maximum=100)
    return q.search(
        _index(),
        kwargs["query"],
        project=kwargs.get("project"),
        kind=kwargs.get("kind"),
        since=kwargs.get("since"),
        until=kwargs.get("until"),
        limit=limit,
    )


def _get_session(**kwargs: Any) -> Any:
    offset = _bounded_int(kwargs, "offset", 0, minimum=0, maximum=1_000_000)
    limit = _bounded_int(kwargs, "limit", 40, minimum=1, maximum=100)
    return q.get_session(
        _index(),
        kwargs["session_id"],
        offset=offset,
        limit=limit,
        kind=kwargs.get("kind"),
    )


def _list_sessions(**kwargs: Any) -> Any:
    limit = _bounded_int(kwargs, "limit", 40, minimum=1, maximum=100)
    return q.list_sessions(
        _index(),
        project=kwargs.get("project"),
        since=kwargs.get("since"),
        limit=limit,
    )


def _find_tool_call(**kwargs: Any) -> Any:
    limit = _bounded_int(kwargs, "limit", 15, minimum=1, maximum=100)
    return q.find_tool_call(
        _index(),
        kwargs["pattern"],
        tool=kwargs.get("tool"),
        session=kwargs.get("session"),
        errors_only=bool(kwargs.get("errors_only", False)),
        limit=limit,
    )


def _get_timeline(**kwargs: Any) -> Any:
    limit = _bounded_int(kwargs, "limit", 25, minimum=1, maximum=100)
    return q.get_timeline(
        _index(), kwargs["topic"], since=kwargs.get("since"), limit=limit
    )


def _synthesize_topic(**kwargs: Any) -> Any:
    max_sessions = _bounded_int(
        kwargs, "max_sessions", 8, minimum=1, maximum=20
    )
    return q.synthesize_topic(
        _index(),
        kwargs["query"],
        max_sessions=max_sessions,
        since=kwargs.get("since"),
    )


def _track_evolution(**kwargs: Any) -> Any:
    return q.track_evolution(_index(), kwargs["topic"], since=kwargs.get("since"))


def _list_credentials(**kwargs: Any) -> Any:
    return v.list_credentials(
        _vault(),
        service=kwargs.get("service"),
        key_name=kwargs.get("key_name"),
        include_placeholders=bool(kwargs.get("include_placeholders", False)),
    )


def _get_credential(**kwargs: Any) -> Any:
    return v.get_credential(
        _vault(), kwargs["key_name"], project=kwargs.get("project"), reveal=True
    )


def _lookup_secret(**kwargs: Any) -> Any:
    return v.lookup_fingerprint(_vault(), kwargs["fingerprint"])


def _code():
    global _code_conn
    if _code_conn is None:
        from . import codeindex

        _code_conn = codeindex.connect()
    return _code_conn


def _find_implementation(**kwargs: Any) -> Any:
    from . import codeindex

    return codeindex.find_implementation(
        _code(),
        kwargs.get("query", ""),
        capability=kwargs.get("capability"),
        project=kwargs.get("project"),
    )


def _list_duplication(**kwargs: Any) -> Any:
    from . import codeindex

    min_count = _bounded_int(kwargs, "min_count", 3, minimum=2, maximum=100)
    return {
        "forks": codeindex.detect_forks(_code()),
        **codeindex.list_duplication(_code(), min_count=min_count),
    }


def _index_stats(**_: Any) -> Any:
    from . import codeindex

    return {
        "sessions": q.stats(_index()),
        "vault": v.stats(_vault()),
        "code": codeindex.stats(_code()),
    }


_KIND_ENUM = [
    "user_instruction",
    "compact_summary",
    "assistant_text",
    "tool_call",
    "tool_error",
]

# capability 清单跟着 config 走，改了配置这里自动同步——写死的话两边会悄悄对不上。
_CAPABILITY_NAMES = "、".join(name for name, _ in config.CODE_CAPABILITY_PATTERNS)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_sessions",
        "description": (
            "全文检索历史 Claude Code session。用于回答「这件事之前是怎么定的」"
            "「上次讨论的结论是什么」。返回片段与定位符，不返回原文全文；要读全文用 get_session。"
            "中文支持两字词精确匹配。结果中的 ⟦SECRET:指纹⟧ 表示原处有凭证，用 lookup_secret 反查。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词，空格分隔多个词为 AND 关系"},
                "project": {"type": "string", "description": "限定项目目录（模糊匹配）"},
                "kind": {"type": "string", "enum": _KIND_ENUM, "description": "限定内容类型。查决策口径优先用 user_instruction"},
                "since": {"type": "string", "description": "起始时间 ISO 格式，如 2026-07-01"},
                "until": {"type": "string", "description": "截止时间 ISO 格式"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "返回条数，默认 15",
                },
            },
            "required": ["query"],
        },
        "handler": _search_sessions,
    },
    {
        "name": "get_session",
        "description": "分页读取单个 session 的完整正文。session_id 支持前 8 位短写。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "session id，可用前 8 位"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000000,
                    "description": "起始段号，默认 0",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "本页段数，默认 40",
                },
                "kind": {"type": "string", "enum": _KIND_ENUM, "description": "只看某一类内容"},
            },
            "required": ["session_id"],
        },
        "handler": _get_session,
    },
    {
        "name": "list_sessions",
        "description": "列出历史 session 元数据（标题、项目、日期、轮数）。用于「我上周在忙什么」这类问题。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "since": {"type": "string", "description": "只看此日期之后的"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "默认 40",
                },
            },
        },
        "handler": _list_sessions,
    },
    {
        "name": "find_tool_call",
        "description": (
            "检索历史执行过的命令与 API 调用（含 Bash 命令原文、文件路径、MCP 方法与入参）。"
            "用于「上次那个脚本怎么跑的」「之前调这个接口的完整参数是什么」。"
            "errors_only=true 可专查失败过的调用，用于回忆踩过的坑。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "匹配词，空格分隔为 AND"},
                "tool": {"type": "string", "description": "限定工具名，如 Bash、Edit、mcp__adspower"},
                "session": {"type": "string", "description": "限定 session id 前缀"},
                "errors_only": {"type": "boolean", "description": "只看失败的调用"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "默认 15",
                },
            },
            "required": ["pattern"],
        },
        "handler": _find_tool_call,
    },
    {
        "name": "get_timeline",
        "description": "按时间顺序还原某个主题的推进过程，每个 session 取命中最强的一条。用于「这件事什么时候开始的、中间发生了什么」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "since": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "默认 25",
                },
            },
            "required": ["topic"],
        },
        "handler": _get_timeline,
    },
    {
        "name": "synthesize_topic",
        "description": (
            "跨 session 收集某主题的素材包，供归纳总结。用于「我在这件事上一共试过几种方案、"
            "各自结论是什么」这类单次检索答不了的问题。按 session 分组返回精选摘录，"
            "已控制总量，可直接读完后自行归纳。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_sessions": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "最多取几个 session，默认 8",
                },
                "since": {"type": "string"},
            },
            "required": ["query"],
        },
        "handler": _synthesize_topic,
    },
    {
        "name": "track_evolution",
        "description": "追踪某个标准/方案随时间的演进，按月分桶给出代表性片段。用于「这个口径改了几版、每版为什么改」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "since": {"type": "string"},
            },
            "required": ["topic"],
        },
        "handler": _track_evolution,
    },
    {
        "name": "list_credentials",
        "description": (
            "列出历史 session 中出现过的凭证与配置变量名录。"
            "**只返回变量名、所属服务、取值个数、最近出现时间，永不返回真实值。**"
            "has_conflict=true 表示该变量存在多个不同取值，取值时需要人工确认。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "如 feishu、discord、adspower"},
                "key_name": {"type": "string", "description": "变量名模糊匹配"},
                "include_placeholders": {"type": "boolean", "description": "是否连占位符一起列出"},
            },
        },
        "handler": _list_credentials,
    },
    {
        "name": "get_credential",
        "description": (
            "取某个变量的真实取值。**这会把凭证带入上下文，仅在确实需要时调用。**"
            "同名多值是常态（多个飞书应用、多张表、开发/生产环境），因此返回全部候选并按"
            "可信度排序（非占位符 > 非本地地址 > 项目匹配 > 最近使用），不会替你挑一个。"
            "usage_example 字段给出当时的调用上下文。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "变量名，如 FEISHU_APP_ID"},
                "project": {"type": "string", "description": "所属项目，用于在多个候选中提权"},
            },
            "required": ["key_name"],
        },
        "handler": _get_credential,
    },
    {
        "name": "lookup_secret",
        "description": "按指纹反查凭证归属。检索结果里出现 ⟦SECRET:a1b2c3⟧ 时，用它查出那是哪个变量（仍不返回值）。",
        "inputSchema": {
            "type": "object",
            "properties": {"fingerprint": {"type": "string", "description": "⟦SECRET:⟧ 中的指纹串"}},
            "required": ["fingerprint"],
        },
        "handler": _lookup_secret,
    },
    {
        "name": "find_implementation",
        "description": (
            "**写任何新集成 / API 客户端 / 工具函数之前先调这个。** 跨全部活跃项目查已有实现。"
            "同时按符号名和外部服务标签检索——名字对不上时标签仍能命中，"
            "这是 grep 做不到的：grep 要求你已经知道该搜哪个字符串（查飞书鉴权得先知道是 "
            "tenant_access_token），而这里按服务名就能找到。"
            f"capability 可选值：{_CAPABILITY_NAMES}。"
            "找到已有实现就复用或扩展，不要重写。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "函数名或能力关键词，如 feishu、send_dm"},
                "capability": {"type": "string", "description": "直接按外部服务标签查"},
                "project": {"type": "string", "description": "限定项目目录（模糊匹配）"},
            },
        },
        "handler": _find_implementation,
    },
    {
        "name": "list_duplication",
        "description": (
            "重复实现清单：哪个能力被实现了几次、哪些同名函数跨项目重复，"
            "以及哪些「项目」其实是同一代码库的多份拷贝（forks 字段）。"
            "用于评估该不该抽共享模块。注意 forks 里的相似可能是刻意并行部署，需人工判断。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_count": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 100,
                    "description": "同名函数至少出现几次才列出，默认 3",
                }
            },
        },
        "handler": _list_duplication,
    },
    {
        "name": "index_stats",
        "description": "索引与凭证库概况：覆盖多少 session、时间跨度、各类内容条数。用于确认索引是否最新。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _index_stats,
    },
]

_HANDLERS: dict[str, Callable[..., Any]] = {tool["name"]: tool["handler"] for tool in TOOLS}
_PUBLIC_TOOLS = [{k: val for k, val in tool.items() if k != "handler"} for tool in TOOLS]


# ---------------------------------------------------------------- JSON-RPC

def _result(request_id: Any, payload: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条请求，返回响应；通知类消息返回 None。"""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": _PUBLIC_TOOLS})

    if method == "tools/call":
        name = params.get("name")
        handler = _HANDLERS.get(str(name))
        if handler is None:
            return _error(request_id, -32602, f"未知工具: {name}")

        arguments = params.get("arguments") or {}
        try:
            payload = handler(**arguments)
        except TypeError as exc:
            return _error(request_id, -32602, f"参数错误: {exc}")
        except Exception as exc:  # 工具内部异常回报给模型，而不是打断连接
            _log(f"工具 {name} 异常: {exc}")
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": f"工具执行失败: {exc}"}],
                    "isError": True,
                },
            )

        text = json.dumps(payload, ensure_ascii=False, indent=1)
        return _result(request_id, {"content": [{"type": "text", "text": text}]})

    if request_id is None:
        return None
    return _error(request_id, -32601, f"未实现的方法: {method}")


def main() -> int:
    _log("启动，等待 stdio 协议消息")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _log("收到非 JSON 输入，已忽略")
            continue

        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
