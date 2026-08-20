"""路径与策略配置。"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
INDEX_DIR = Path(os.path.expanduser("~/.claude/session-index"))
INDEX_DB = INDEX_DIR / "index.db"
VAULT_DB = INDEX_DIR / "vault.db"
CODE_DB = INDEX_DIR / "code.db"

# 知识目录。where-things-are.md 由扫描生成、每次覆盖；canonical.json 是手写的
# 「哪个实现是规范实现」，生成器只读不写——否则每天刷新会把人工标注冲掉。
KNOWLEDGE_DIR = Path(os.path.expanduser("~/.claude/knowledge"))
KNOWLEDGE_MAP = KNOWLEDGE_DIR / "where-things-are.md"
KNOWLEDGE_CANONICAL = KNOWLEDGE_DIR / "canonical.json"


# ------------------------------------------------------------ 代码索引范围

# 要索引哪些项目目录，默认自动推导：从 session 索引里查最近有会话的工作目录。
# 不写死清单是刻意的——写死的清单会随着项目增删而悄悄过期，而且换台机器就全错。
CODE_PROJECT_DAYS = 45          # 只认最近这么多天有过会话的目录
CODE_PROJECT_MIN_SESSIONS = 2   # 只开过一次会话的多半是路过，不算活跃项目
CODE_PROJECT_LIMIT = 30         # 上限，防止把整个 home 扫一遍

CODE_EXTENSIONS = (".py", ".js", ".ts", ".tsx", ".mjs", ".jsx")

CODE_SKIP_DIRS = frozenset(
    {
        "node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build",
        ".next", "site-packages", ".pytest_cache", ".mypy_cache", "coverage",
        "vendor", "third_party", ".claude",
    }
)

# 单文件上限，超过的多半是压缩产物或生成代码
CODE_MAX_FILE_BYTES = 400_000


def _home_guard() -> set[Path]:
    """不该被当作项目根的目录：home 本身、根目录、以及常见的收纳目录。

    漏掉这层会出事：cwd 是 ~ 的会话很常见（随手开一个问问题），
    把 ~ 当项目根会导致递归扫描整个 home。
    """
    home = Path.home()
    return {
        Path("/"),
        home,
        home / "Desktop",
        home / "Downloads",
        home / "Documents",
    }


def code_project_dirs() -> tuple[str, ...]:
    """要做代码索引的项目目录。

    默认从 session 索引里推导：最近 CODE_PROJECT_DAYS 天内、开过至少
    CODE_PROJECT_MIN_SESSIONS 次会话的 cwd，按会话数排序取前 N 个。
    这比手写清单准，且会自动跟着项目增删走。

    想手动指定就设环境变量（冒号分隔，跟 PATH 一个格式）：
        export SESSION_KNOWLEDGE_PROJECT_DIRS="~/proj-a:~/proj-b"
    """
    override = os.environ.get("SESSION_KNOWLEDGE_PROJECT_DIRS", "").strip()
    if override:
        return tuple(p for p in override.split(":") if p.strip())

    if not INDEX_DB.exists():
        return ()

    cutoff = f"-{CODE_PROJECT_DAYS} days"
    try:
        conn = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return ()

    try:
        rows = conn.execute(
            """
            SELECT cwd, COUNT(*) AS n
            FROM sessions
            WHERE cwd != ''
              AND ended_at >= datetime('now', ?)
            GROUP BY cwd
            HAVING n >= ?
            ORDER BY n DESC
            LIMIT ?
            """,
            (cutoff, CODE_PROJECT_MIN_SESSIONS, CODE_PROJECT_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        conn.close()

    guard = _home_guard()
    return tuple(cwd for cwd, _ in rows if Path(cwd) not in guard)


# 外部服务能力标签。这是「不用向量也能语义检索」的关键——
# 问题从来不是「找不到叫 get_xxx_token 的函数」，而是「不知道有人实现过这个服务的鉴权」。
# 按服务打标签比按名字搜有效得多，且零 embedding 成本。
#
# 这份清单是给你改的：删掉用不上的，加上自己在用的服务。格式是 (标签, 正则)，
# 正则匹配文件内容，命中就给该文件打上这个标签。
CODE_CAPABILITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("feishu", r"tenant_access_token|open\.feishu\.cn|/open-apis/|bitable/v1|larksuite"),
    # 只认真正调 API 的，不认单纯提到名字的。裸 `adspower` 会把前端页面里的
    # 文案和注释一起标上——实测大部分命中文件只是提及，不是调用。
    ("adspower", r"local\.adspower|:50325|api/v[12]/browser|adspower-local-api"),
    ("discord", r"discord\.com/api|discord\.py|discord\.js|import discord|from discord"),
    ("imap", r"imaplib|imap\.[a-z]+\.[a-z]+|IMAP4_SSL"),
    ("curl_cffi", r"curl_cffi"),
    ("postgres", r"psycopg|postgresql://|asyncpg"),
    ("openai", r"api\.openai\.com|from openai|import openai"),
    ("anthropic", r"api\.anthropic\.com|from anthropic|import anthropic"),
    ("gemini", r"generativelanguage|genai|gemini-\d"),
    ("browser_automation", r"playwright|selenium|puppeteer|webdriver"),
    ("aws", r"boto3|botocore|amazonaws\.com"),
    ("stripe", r"stripe\.|api\.stripe\.com"),
    ("supabase", r"supabase|createClient\("),
)

# 工具结果只留头部这么多字符；报错则全文保留（排错时需要完整堆栈）
TOOL_RESULT_HEAD = 512

# 单条 chunk 上限，超长正文切分入库，避免单行几万字撑爆检索结果
MAX_CHUNK_CHARS = 4000


# ------------------------------------------------------------ 私人内容排除

# 明确不想进索引的 session 标题，精确匹配。空着也能用——下面的启发式会兜底。
# 例：{"月度财务预算评估", "体检报告解读"}
PRIVATE_TITLES: frozenset[str] = frozenset()

# 标题名单只能覆盖你已经知道的。新产生的私人 session 靠这组启发式兜底：
# 私人词命中够多、且明显压过工作词，就判定为私人，不进索引。
_PRIVATE_KEYWORDS = re.compile(
    r"记账|账单|微信支付|支付宝|消费记录|工资|薪资|薪水|月薪|房租|报销"
    r"|银行卡|花呗|余额宝|个人所得税|体检|病历|医院|保险|公积金|个税"
    r"|payslip|salary|mortgage|medical record|tax return",
    re.I,
)

_WORK_KEYWORDS = re.compile(
    r"api|脚本|数据库|部署|重构|调试|报错|接口|前端|后端|测试|日志|周报"
    r"|script|database|deploy|refactor|debug|endpoint|migration|schema",
    re.I,
)

PRIVATE_HIT_THRESHOLD = 5


def looks_private(title: str, user_text: str) -> bool:
    """判断一个 session 是否应作为私人内容排除。"""
    if title and title.strip() in PRIVATE_TITLES:
        return True

    private_hits = len(_PRIVATE_KEYWORDS.findall(user_text))
    if private_hits < PRIVATE_HIT_THRESHOLD:
        return False

    work_hits = len(_WORK_KEYWORDS.findall(user_text))
    return work_hits < private_hits
