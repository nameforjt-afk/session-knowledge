"""凭证识别、脱敏、抽取。

这一个模块同时服务两个方向相反的需求：

  索引侧  —— 把密钥从正文里抹掉，换成 ⟦SECRET:指纹⟧。检索结果会大量进入模型
            上下文，全文索引里绝不能留明文。
  凭证库侧 —— 把 KEY=VALUE 完整抽出来存进独立的 vault.db（0600）。

两侧共用同一套模式定义，所以脱敏漏掉的东西不会在 vault 里凭空出现，反之亦然。
索引里的指纹就是 vault 的外键：搜到 ⟦SECRET:a1b2c3⟧ 就知道「这里有个凭证」，
拿值需要显式再查一次。

重要：抽取必须逐行解析结构化 JSON 后在字段内匹配。实测直接对文件原文跑正则会
跨行吞进注释，抽出 `cli_xxxxxx\\n#...` 这种脏值。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["secret", "identifier"]

REDACTION_PREFIX = "⟦SECRET:"
REDACTION_SUFFIX = "⟧"


# ---------------------------------------------------------------- 变量名分类

# 这些名字虽然带 TOKEN/KEY 字样，实际是标识符不是密钥。飞书多维表格的 app_token
# 就是文档 ID，把它抹掉会让「哪个 session 用了哪张表」查不出来。
_IDENTIFIER_NAME = re.compile(
    r"(?:BITABLE_APP_TOKEN|APP_TOKEN|TABLE_ID|GUILD_ID|CHANNEL_ID|APP_ID|APPID"
    r"|CLIENT_ID|BUTTON_ID|_URL|_BASE|_HOST|_ENDPOINT|_EMAIL|_USER|_NAME|_PATH"
    r"|_PORT|_REGION|_BUCKET|_VERSION)$",
    re.I,
)

_SECRET_NAME = re.compile(
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY"
    r"|_KEY|COOKIE|CREDENTIAL|AUTH|SESSION_?ID|SIGNATURE|SALT)",
    re.I,
)

# 前端公开变量，构建时就会打进 bundle，不算密钥
_PUBLIC_NAME = re.compile(r"^(?:NEXT_PUBLIC_|VITE_|PUBLIC_|REACT_APP_)", re.I)


def classify_key(key_name: str) -> Kind:
    """判断一个变量名装的是密钥还是标识符。"""
    if _PUBLIC_NAME.match(key_name):
        return "identifier"
    if _IDENTIFIER_NAME.search(key_name):
        return "identifier"
    if _SECRET_NAME.search(key_name):
        return "secret"
    return "identifier"


# ---------------------------------------------------------------- 服务归属

_SERVICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("feishu", re.compile(r"FEISHU|LARK|BITABLE|WIKI", re.I)),
    ("discord", re.compile(r"DISCORD|GUILD|CHANNEL", re.I)),
    ("adspower", re.compile(r"ADSPOWER", re.I)),
    ("openai", re.compile(r"OPENAI|GPT", re.I)),
    ("anthropic", re.compile(r"ANTHROPIC|CLAUDE", re.I)),
    ("gemini", re.compile(r"GEMINI|GOOGLE", re.I)),
    ("dashscope", re.compile(r"DASHSCOPE|QWEN", re.I)),
    ("reddit", re.compile(r"REDDIT", re.I)),
    ("youtube", re.compile(r"YOUTUBE", re.I)),
    ("github", re.compile(r"GITHUB|^GH_", re.I)),
    ("postgres", re.compile(r"POSTGRES|DATABASE_URL|^PG", re.I)),
    ("capsolver", re.compile(r"CAPSOLVER|CAPTCHA", re.I)),
    ("aws", re.compile(r"AWS|^S3_|CLOUDFRONT", re.I)),
    ("stripe", re.compile(r"STRIPE", re.I)),
    ("supabase", re.compile(r"SUPABASE", re.I)),
    ("slack", re.compile(r"SLACK", re.I)),
    ("llm_proxy", re.compile(r"LLM_PROXY|LLM_AUTH", re.I)),
)


def service_of(key_name: str, value: str = "") -> str:
    """推断变量属于哪个服务，认不出返回 other。"""
    for service, pattern in _SERVICE_PATTERNS:
        if pattern.search(key_name):
            return service
    for service, pattern in _SERVICE_PATTERNS:
        if value and pattern.search(value):
            return service
    return "other"


# ---------------------------------------------------------------- 占位符识别

_PLACEHOLDER_MARKERS = (
    "xxx",
    "your_",
    "your-",
    "yourkey",
    "yourtoken",
    "placeholder",
    "example.com",
    "changeme",
    "change_me",
    "todo",
    "fixme",
    "replace_me",
    "<your",
    "dummy",
    "sample",
    "test_key",
    "abc123",
    "foo",
    "bar",
)

_TEMPLATE_SYNTAX = re.compile(r"^\$|^\{\{|^<|\$\{|^\.\.\.|^\*+$")


def is_redacted(value: str) -> bool:
    """值是否已经是脱敏令牌。

    没有这个判断，对已脱敏文本再跑一次扫描会把 KEY=⟦SECRET:指纹⟧ 里的令牌
    当成新的明文密钥，verify-redaction 将永远报失败。
    """
    return REDACTION_PREFIX in value


def is_placeholder(value: str) -> bool:
    """判断是不是占位符而非真实凭证。

    实测 session 里大量 cli_xxxxxx / YOUR_SERVER / your-proxy / localhost
    跟真值混在一起，不区分的话凭证库会满是垃圾。
    """
    stripped = value.strip()
    if not stripped or len(stripped) < 4:
        return True
    if is_redacted(stripped):
        return True
    if _TEMPLATE_SYNTAX.search(stripped):
        return True

    lowered = stripped.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return True
    if lowered in ("none", "null", "true", "false", "undefined", "nil", "empty"):
        return True
    # 全同一个字符，如 "0000000000"
    if len(set(stripped)) <= 2 and len(stripped) > 5:
        return True
    return False


def is_local_url(value: str) -> bool:
    """本地/回环地址，记录但不该被当成生产配置推荐。"""
    return bool(re.match(r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])", value, re.I))


# ---------------------------------------------------------------- 独立密钥字面量

# 每条：(名称, 正则, 密钥所在的捕获组号；0 表示整段匹配)
_SECRET_LITERALS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("discord_bot_token", re.compile(r"\b[MNO][A-Za-z0-9_\-]{22,26}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{25,}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), 0),
    ("openai_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b"), 0),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), 0),
    ("aws_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("bearer", re.compile(r"[Bb]earer\s+([A-Za-z0-9._\-]{20,})"), 1),
    ("feishu_webhook", re.compile(r"open-apis/bot/v2/hook/([A-Za-z0-9\-]{16,})"), 1),
    ("basic_auth_url", re.compile(r"://[^/\s:@]{1,64}:([^/\s:@]{4,})@"), 1),
)


# ---------------------------------------------------------------- 赋值抽取

_ENV_ASSIGN = re.compile(
    r"(?:^|[\s;&|(\[{,])(?:export\s+)?([A-Z][A-Z0-9_]{2,48})\s*=\s*"
    r"(\"[^\"\n]{1,400}\"|'[^'\n]{1,400}'|[^\s\"'\n,;)\]}]{1,400})"
)

_JSON_ASSIGN = re.compile(
    r"[\"']([A-Za-z][A-Za-z0-9_\-]{2,48})[\"']\s*:\s*[\"']([^\"'\n]{1,400})[\"']"
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip().rstrip(",;\\")


def fingerprint(value: str) -> str:
    """凭证值的稳定短指纹，索引与 vault 之间靠它对应。"""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Assignment:
    """一次 KEY=VALUE 观测。

    context 是赋值处前后的原文片段（已脱敏），用来回答「当时怎么用的」——
    凭证登记表里最有价值的一列往往不是值本身，而是它出现在哪条命令里。
    """

    key_name: str
    value: str
    kind: Kind
    service: str
    fingerprint: str
    is_placeholder: bool
    context: str = ""


_CONTEXT_WIDTH = 120

# 键名必须自身看起来像凭证或连接配置才登记。
#
# 曾经试过「全大写下划线名一律放行」，结果 LOG= / API= / CTA= / P70= 这类正文里的
# 缩写全被收进来。反过来，只认这组词也不会漏——实测 112 个真实变量名（ADSPOWER_API_KEY、
# FEISHU_APP_SECRET、DISCORD_TOKEN、DATABASE_URL、GUILD_ID、STEP_EMAIL…）全部命中。
_CREDENTIAL_WORD = re.compile(
    # auth(?!or)：author / author_id / author_handle 是创作者数据，不是认证信息。
    # 不加这个否定环视，`auth` 会作为子串匹配进 `author`，把整批作者字段误判成密钥。
    r"(?:secret|token|password|passwd|api_?key|_key$|^key$|private|cookie"
    r"|credential|auth(?!or)|app_?id|client_?id|guild_?id|channel_?id|table_?id"
    r"|webhook|_url$|_uri$|_host$|_base$|_port$|database|dsn|username"
    r"|_user$|_email$|proxy_)",
    re.I,
)

# 这些词单独出现时含义取决于上下文：小写的 `username` / `account` 绝大多数是接口
# 返回里的业务数据（一次 API 调用就能带回几百条），而全大写的 USERNAME / ACCOUNT
# 才是环境变量。带前后缀的（DB_USERNAME、proxy_user）不受影响。
_AMBIGUOUS_ALONE = frozenset({"username", "user", "account", "email", "name", "host", "id"})


def _is_credential_name(key_name: str) -> bool:
    """判断这个键名值不值得进凭证登记表。"""
    if key_name.lower() in _AMBIGUOUS_ALONE and not key_name.isupper():
        return False
    return bool(_CREDENTIAL_WORD.search(key_name))


def extract_assignments(text: str) -> list[Assignment]:
    """从一段文本里抽出所有 KEY=VALUE / "key": "value"。

    调用方必须先把 JSONL 解析成字段再传进来，不要直接喂整个文件——实测对文件
    原文跑正则会跨行吞进注释，抽出 `cli_xxxxxx\\n#...` 这种脏值。
    """
    seen: set[tuple[str, str]] = set()
    out: list[Assignment] = []

    for pattern in (_ENV_ASSIGN, _JSON_ASSIGN):
        for match in pattern.finditer(text):
            key_name = match.group(1)
            if not _is_credential_name(key_name):
                continue
            value = _unquote(match.group(2))
            if not value:
                continue
            # 值本身又是个变量引用（KEY=$OTHER_KEY），没有信息量
            if value.startswith("$") or value.startswith("%"):
                continue
            dedupe = (key_name, value)
            if dedupe in seen:
                continue
            seen.add(dedupe)

            start = max(0, match.start() - _CONTEXT_WIDTH // 2)
            end = min(len(text), match.end() + _CONTEXT_WIDTH // 2)
            context = redact(text[start:end])[0].replace("\n", " ").strip()

            out.append(
                Assignment(
                    key_name=key_name,
                    value=value,
                    kind=classify_key(key_name),
                    service=service_of(key_name, value),
                    fingerprint=fingerprint(value),
                    is_placeholder=is_placeholder(value),
                    context=context,
                )
            )
    return out


# ---------------------------------------------------------------- 脱敏

@dataclass(frozen=True)
class Finding:
    """一处需要抹掉的密钥。"""

    label: str
    value: str
    fingerprint: str
    start: int
    end: int


# 已脱敏的令牌本身含有冒号，会被 basic_auth_url 一类的模式重新匹配——
# https://⟦SECRET:abc⟧@host 会被读成「用户名 ⟦SECRET、密码 abc⟧」。不排除掉的话
# 自检会对着自己的替换结果反复报警，误报的自检比没有自检更糟。
_REDACTION_TOKEN = re.compile(
    re.escape(REDACTION_PREFIX) + r"[0-9a-f]{6,}" + re.escape(REDACTION_SUFFIX)
)


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _REDACTION_TOKEN.finditer(text)]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _collect_findings(text: str) -> list[Finding]:
    findings: list[Finding] = []
    skip = _token_spans(text)

    # 1. 独立密钥字面量
    for label, pattern, group in _SECRET_LITERALS:
        for match in pattern.finditer(text):
            value = match.group(group)
            if not value or is_placeholder(value):
                continue
            if _overlaps(match.start(group), match.end(group), skip):
                continue
            findings.append(
                Finding(
                    label=label,
                    value=value,
                    fingerprint=fingerprint(value),
                    start=match.start(group),
                    end=match.end(group),
                )
            )

    # 2. 变量名判定为密钥的赋值
    for pattern in (_ENV_ASSIGN, _JSON_ASSIGN):
        for match in pattern.finditer(text):
            key_name = match.group(1)
            if classify_key(key_name) != "secret":
                continue
            raw = match.group(2)
            value = _unquote(raw)
            if not value or is_placeholder(value):
                continue
            # 定位真实值在原文中的位置（跳过可能存在的引号）
            offset = raw.find(value)
            start = match.start(2) + (offset if offset >= 0 else 0)
            if _overlaps(start, start + len(value), skip):
                continue
            findings.append(
                Finding(
                    label=key_name,
                    value=value,
                    fingerprint=fingerprint(value),
                    start=start,
                    end=start + len(value),
                )
            )

    return findings


def _merge_spans(findings: list[Finding]) -> list[Finding]:
    """合并重叠区间，保留先出现那条的指纹并把终点延伸到并集。

    直接丢弃后一条是不安全的：若 A=[10,50]、B=[30,80]，丢掉 B 会让 50~80 这段
    密钥原样留在正文里。延伸终点则保证整片区域都被覆盖。
    """
    ordered = sorted(findings, key=lambda f: (f.start, -(f.end - f.start)))
    merged: list[Finding] = []

    for finding in ordered:
        if merged and finding.start < merged[-1].end:
            previous = merged[-1]
            if finding.end > previous.end:
                merged[-1] = Finding(
                    label=previous.label,
                    value=previous.value,
                    fingerprint=previous.fingerprint,
                    start=previous.start,
                    end=finding.end,
                )
            continue
        merged.append(finding)

    return merged


def redact(text: str) -> tuple[str, list[Finding]]:
    """把文本里的密钥替换成 ⟦SECRET:指纹⟧。

    返回 (脱敏后文本, 命中列表)。标识符（app_id / table_id / URL）保持原样——
    它们不是密钥，且是有价值的检索锚点。
    """
    findings = _merge_spans(_collect_findings(text))
    if not findings:
        return text, []

    pieces: list[str] = []
    cursor = 0
    for finding in findings:
        pieces.append(text[cursor : finding.start])
        pieces.append(f"{REDACTION_PREFIX}{finding.fingerprint}{REDACTION_SUFFIX}")
        cursor = finding.end
    pieces.append(text[cursor:])

    return "".join(pieces), findings


def scan_for_leaks(text: str) -> list[str]:
    """自检：返回文本中仍存在的明文密钥标签。

    verify-redaction 命令对整个 index.db 跑这个，结果必须为空。
    """
    return [finding.label for finding in _collect_findings(text)]
