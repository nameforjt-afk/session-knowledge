"""扫描项目目录下的 .env 文件，作为凭证的权威来源。

session 里抽到的凭证是「当时泄漏进对话的快照」，可能早已轮换；.env 是当前真正在
用的值。两者可信度不是一个级别，所以分开记 source，取值时 .env 优先。

.env.example / .env.sample / .env.template 是模板，值一律按占位符登记——它们的
价值在于告诉你「这个项目需要哪些变量」，而不是变量的值。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .redact import (
    Assignment,
    _is_credential_name,
    classify_key,
    fingerprint,
    is_local_url,
    is_placeholder,
    service_of,
)

# 模板文件名后缀——这些文件里的值不是真值
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".tpl", ".dist")

# 不下钻的目录
_SKIP_DIRS = frozenset(
    {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target"}
)

# KEY=VALUE，允许 export 前缀、行内注释、单双引号
_ENV_LINE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
)

MAX_ENV_BYTES = 2_000_000


@dataclass(frozen=True)
class EnvFile:
    path: Path
    project_dir: Path
    is_template: bool


def is_template(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _TEMPLATE_SUFFIXES)


def find_env_files(project_dirs: list[Path]) -> list[EnvFile]:
    """在给定项目目录下查找 .env 系列文件。"""
    found: list[EnvFile] = []
    seen: set[Path] = set()

    for root in project_dirs:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                if not filename.startswith(".env"):
                    continue
                path = Path(dirpath) / filename
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen or not path.is_file():
                    continue
                if path.stat().st_size > MAX_ENV_BYTES:
                    continue
                seen.add(resolved)
                found.append(EnvFile(path=path, project_dir=root, is_template=is_template(path)))

    return found


def _strip_value(raw: str) -> str:
    """去掉引号与行内注释。

    只在值**没有**被引号包裹时才剥离 # 之后的内容——带引号的值里 # 是合法字符
    （密码里出现 # 很常见），一刀切会把密码截断。
    """
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]

    hash_pos = value.find(" #")
    if hash_pos >= 0:
        value = value[:hash_pos]
    if value.startswith("#"):
        return ""
    return value.strip()


def parse_env_file(env: EnvFile) -> Iterator[Assignment]:
    """解析单个 .env，产出凭证观测。"""
    try:
        content = env.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None:
            continue

        key_name = match.group(1)
        if not _is_credential_name(key_name):
            continue

        value = _strip_value(match.group(2))
        if not value or value.startswith("$"):
            continue

        yield Assignment(
            key_name=key_name,
            value=value,
            kind=classify_key(key_name),
            service=service_of(key_name, value),
            fingerprint=fingerprint(value),
            # 模板文件里的值一律不当真值
            is_placeholder=env.is_template or is_placeholder(value),
            context=f"{env.path.name} @ {env.project_dir.name}",
        )


def scan(project_dirs: list[Path]) -> list[tuple[EnvFile, list[Assignment]]]:
    """扫描全部 .env，返回 (文件, 观测列表)。

    真值文件排在模板之前。同一个值可能同时出现在 .env 和 .env.example 里，先写入
    的那条决定了这条记录的初始来源路径，顺序反了就会显示成来自模板文件。
    """
    results: list[tuple[EnvFile, list[Assignment]]] = []
    for env in sorted(find_env_files(project_dirs), key=lambda e: (e.is_template, str(e.path))):
        assignments = list(parse_env_file(env))
        if assignments:
            results.append((env, assignments))
    return results


def local_flag(value: str) -> bool:
    return is_local_url(value)
