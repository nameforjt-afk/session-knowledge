"""跨项目代码索引：写新东西之前先查有没有人写过。

存在的理由是一组实测数字：这台机器上 39 个文件各自实现了飞书鉴权（跨 11 个项目），
30 个实现了多维表格读写，23 个实现了 AdsPower 调用，`generate_dm` 被定义了 40 次，
而且没有任何共享库。写的时候根本不知道另外 39 个存在。

索引三层，其中第二层才是关键：

  symbols       函数/类定义 + 文件:行号 + 签名
  capabilities  每个文件碰了哪些外部服务（feishu / adspower / discord …）
  imports       已引入的第三方库

真实问题从来不是「找不到叫 get_feishu_token 的函数」——名字千奇百怪，按名字搜必然
漏。而是「不知道有人实现过飞书鉴权」。按服务打标签能直接回答后者，且零 embedding
成本。这也是这里不上向量检索的原因：中文加大量专有名词，关键词加标签比 embedding 稳。

Python 走 ast 解析（拿得到准确签名和类方法），JS/TS 走正则——语法太多变体，
正则的召回反而比半吊子的解析器高。
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import config
from .dbio import prepare_private_database, restrict_sqlite_artifacts

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS files (
    file_id    INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    project    TEXT NOT NULL,
    lang       TEXT NOT NULL,
    lines      INTEGER NOT NULL DEFAULT 0,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    line      INTEGER NOT NULL DEFAULT 0,
    signature TEXT NOT NULL DEFAULT '',
    parent    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS capabilities (
    file_id    INTEGER NOT NULL,
    capability TEXT NOT NULL,
    PRIMARY KEY (file_id, capability)
);

CREATE TABLE IF NOT EXISTS imports (
    file_id INTEGER NOT NULL,
    module  TEXT NOT NULL,
    PRIMARY KEY (file_id, module)
);

CREATE INDEX IF NOT EXISTS idx_sym_name  ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_file  ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_cap       ON capabilities(capability);
CREATE INDEX IF NOT EXISTS idx_imp       ON imports(module);
CREATE INDEX IF NOT EXISTS idx_files_prj ON files(project);
"""

_CAPABILITY_RE = tuple(
    (name, re.compile(pattern, re.I)) for name, pattern in config.CODE_CAPABILITY_PATTERNS
)

# JS/TS 的函数声明变体太多，正则覆盖常见四种：function 声明、const 箭头函数、
# class 方法、对象方法简写。语法解析器处理不了 TSX 又要额外依赖，得不偿失。
_JS_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:(?P<kw>async\s+function|function|class)\s+(?P<name1>[A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+(?P<name2>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"
    r"|(?P<name3>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{)",
    re.M,
)

_JS_IMPORT = re.compile(
    r"""(?:from\s+['"](?P<m1>[^'".][^'"]*)['"]|require\(\s*['"](?P<m2>[^'".][^'"]*)['"]\s*\))"""
)


@dataclass
class ParsedFile:
    path: str
    project: str
    lang: str
    lines: int
    mtime: float
    size: int
    symbols: list[tuple[str, str, int, str, str]] = field(default_factory=list)
    capabilities: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or config.CODE_DB
    prepare_private_database(target)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    restrict_sqlite_artifacts(target)
    return conn


# ---------------------------------------------------------------- 遍历

def project_roots() -> list[Path]:
    """展开配置里的项目目录，去掉不存在的和被父目录包含的。"""
    expanded = sorted(
        {Path(os.path.expanduser(p)) for p in config.code_project_dirs()},
        key=lambda p: len(str(p)),
    )
    kept: list[Path] = []
    for root in expanded:
        if not root.is_dir():
            continue
        if any(str(root).startswith(f"{parent}{os.sep}") for parent in kept):
            continue
        kept.append(root)
    return kept


def iter_code_files(roots: list[Path] | None = None) -> Iterator[tuple[Path, str]]:
    """产出 (文件路径, 所属项目)。"""
    for root in roots if roots is not None else project_roots():
        label = str(root).replace(os.path.expanduser("~"), "~")
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in config.CODE_SKIP_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                if not filename.endswith(config.CODE_EXTENSIONS):
                    continue
                path = Path(dirpath) / filename
                try:
                    if path.stat().st_size > config.CODE_MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                yield path, label


# ---------------------------------------------------------------- 解析

def _python_symbols(text: str) -> list[tuple[str, str, int, str, str]]:
    """用 ast 抽 Python 符号，拿到准确签名和所属类。

    语法错误的文件退回空列表——半仓库的实验脚本跑不通是常态，不该让整个索引失败。
    """
    # 别人代码里的坏转义（"\s" 之类）会让 ast.parse 冒 SyntaxWarning 到 stderr。
    # 那是被扫描文件的问题，不是这里的问题，压掉以免污染输出。
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []

    out: list[tuple[str, str, int, str, str]] = []

    def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        args = [a.arg for a in node.args.args]
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        args.extend(a.arg for a in node.args.kwonlyargs)
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        return f"({', '.join(args)})"

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out.append((node.name, "class", node.lineno, "", ""))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((child.name, "method", child.lineno, signature(child), node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 类内方法已在上面收过，这里只要模块级函数
            out.append((node.name, "function", node.lineno, signature(node), ""))

    # 去掉重复收进来的类方法（ast.walk 会再访问一次）
    seen: set[tuple[str, int]] = set()
    unique: list[tuple[str, str, int, str, str]] = []
    for item in out:
        key = (item[0], item[2])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _python_imports(text: str) -> set[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def _js_symbols(text: str) -> list[tuple[str, str, int, str, str]]:
    out: list[tuple[str, str, int, str, str]] = []
    for match in _JS_SYMBOL.finditer(text):
        name = match.group("name1") or match.group("name2") or match.group("name3")
        if not name or name in ("if", "for", "while", "switch", "catch", "return"):
            continue
        kind = "class" if (match.group("kw") or "").startswith("class") else "function"
        line = text.count("\n", 0, match.start()) + 1
        out.append((name, kind, line, "", ""))
    return out


def parse_file(path: Path, project: str) -> ParsedFile | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        stat = path.stat()
    except OSError:
        return None

    lang = "python" if path.suffix == ".py" else "javascript"
    parsed = ParsedFile(
        path=str(path),
        project=project,
        lang=lang,
        lines=text.count("\n") + 1,
        mtime=stat.st_mtime,
        size=stat.st_size,
    )

    if lang == "python":
        parsed.symbols = _python_symbols(text)
        parsed.imports = _python_imports(text)
    else:
        parsed.symbols = _js_symbols(text)
        parsed.imports = {
            m.group("m1") or m.group("m2") for m in _JS_IMPORT.finditer(text)
        } - {None}

    for name, pattern in _CAPABILITY_RE:
        if pattern.search(text):
            parsed.capabilities.add(name)

    return parsed


# ---------------------------------------------------------------- 写入

class CodeWriter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def needs_reindex(self, path: Path) -> bool:
        row = self.conn.execute(
            "SELECT mtime, size FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        if row is None:
            return True
        try:
            stat = path.stat()
        except OSError:
            return False
        return row["mtime"] != stat.st_mtime or row["size"] != stat.st_size

    def drop_file(self, path: str) -> None:
        row = self.conn.execute("SELECT file_id FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return
        file_id = row["file_id"]
        for table in ("symbols", "capabilities", "imports"):
            self.conn.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))

    def write(self, parsed: ParsedFile) -> None:
        self.drop_file(parsed.path)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO files (path, project, lang, lines, mtime, size, indexed_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                parsed.path,
                parsed.project,
                parsed.lang,
                parsed.lines,
                parsed.mtime,
                parsed.size,
                time.strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )
        file_id = cursor.lastrowid

        cursor.executemany(
            "INSERT INTO symbols (file_id, name, kind, line, signature, parent) VALUES (?,?,?,?,?,?)",
            [(file_id, *sym) for sym in parsed.symbols],
        )
        cursor.executemany(
            "INSERT OR IGNORE INTO capabilities (file_id, capability) VALUES (?,?)",
            [(file_id, cap) for cap in parsed.capabilities],
        )
        cursor.executemany(
            "INSERT OR IGNORE INTO imports (file_id, module) VALUES (?,?)",
            [(file_id, mod) for mod in parsed.imports if mod],
        )

    def prune_missing(self) -> int:
        """删掉已从磁盘消失的文件记录，返回清理条数。"""
        gone = [
            row["path"]
            for row in self.conn.execute("SELECT path FROM files")
            if not os.path.exists(row["path"])
        ]
        for path in gone:
            self.drop_file(path)
        return len(gone)


# ---------------------------------------------------------------- 查询

def _rel(path: str) -> str:
    return path.replace(os.path.expanduser("~"), "~")


def find_implementation(
    conn: sqlite3.Connection,
    query: str,
    *,
    capability: str | None = None,
    project: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """查已有实现。

    同时按符号名和能力标签检索——名字对不上时标签仍能命中，这是能找到
    「有人实现过飞书鉴权」的关键。
    """
    terms = [t for t in query.split() if t]
    results: dict[str, Any] = {"query": query, "symbols": [], "by_capability": []}

    if terms:
        clauses = " AND ".join(["s.name LIKE ?"] * len(terms))
        params: list[Any] = [f"%{t}%" for t in terms]
        extra = ""
        if project:
            extra += " AND f.project LIKE ?"
            params.append(f"%{project}%")
        rows = conn.execute(
            f"""
            SELECT s.name, s.kind, s.line, s.signature, s.parent, f.path, f.project
            FROM symbols s JOIN files f ON f.file_id = s.file_id
            WHERE {clauses}{extra}
            ORDER BY s.name, f.project
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        results["symbols"] = [
            {
                "name": r["name"],
                "kind": r["kind"],
                "parent": r["parent"],
                "signature": r["signature"],
                "location": f"{_rel(r['path'])}:{r['line']}",
                "project": r["project"],
            }
            for r in rows
        ]

    # 能力标签：查询词本身命中标签名，或调用方显式指定
    wanted = {capability} if capability else {
        name for name, _ in config.CODE_CAPABILITY_PATTERNS
        if any(t.lower() in name or name in t.lower() for t in terms)
    }
    for cap in sorted(w for w in wanted if w):
        rows = conn.execute(
            """
            SELECT f.path, f.project, f.lines FROM capabilities c
            JOIN files f ON f.file_id = c.file_id
            WHERE c.capability = ?
            ORDER BY f.lines DESC LIMIT ?
            """,
            (cap, limit),
        ).fetchall()
        if rows:
            results["by_capability"].append(
                {
                    "capability": cap,
                    "file_count": len(rows),
                    "files": [
                        {"location": _rel(r["path"]), "project": r["project"], "lines": r["lines"]}
                        for r in rows
                    ],
                }
            )
    return results


def list_duplication(conn: sqlite3.Connection, *, min_count: int = 3) -> dict[str, Any]:
    """哪些能力和符号被重复实现了几次、分别在哪。

    这同时是「把轮子收敛成共享库」那一步的工作清单——不用另做调研。
    """
    caps = [
        {
            "capability": r["capability"],
            "files": r["n"],
            "projects": r["p"],
        }
        for r in conn.execute(
            """
            SELECT c.capability, COUNT(*) n, COUNT(DISTINCT f.project) p
            FROM capabilities c JOIN files f ON f.file_id = c.file_id
            GROUP BY c.capability ORDER BY n DESC
            """
        )
    ]

    # 测试脚手架和通用小工具名不算重复造轮子
    noise = (
        "main", "setUp", "tearDown", "__init__", "run", "test", "close", "log",
        "to_dict", "from_dict", "sleep", "now", "handler", "index", "get", "post",
    )
    syms = [
        {"name": r["name"], "definitions": r["n"], "projects": r["p"]}
        for r in conn.execute(
            f"""
            SELECT s.name, COUNT(*) n, COUNT(DISTINCT f.project) p
            FROM symbols s JOIN files f ON f.file_id = s.file_id
            WHERE s.kind IN ('function','class') AND s.name NOT IN ({','.join('?' * len(noise))})
            GROUP BY s.name HAVING n >= ? AND p >= 2
            ORDER BY p DESC, n DESC LIMIT 40
            """,
            [*noise, min_count],
        )
    ]
    return {"by_capability": caps, "cross_project_symbols": syms}


# ---------------------------------------------------------------- 知识文件生成

def _load_canonical() -> dict[str, str]:
    """读手写的「规范实现」标注。

    刻意跟生成的 markdown 分开存：生成器每天覆盖 where-things-are.md，人工标注若
    写在同一个文件里会被冲掉。哪个实现算规范只有人知道，索引推不出来。
    """
    if not config.KNOWLEDGE_CANONICAL.exists():
        return {}
    try:
        import json

        data = json.loads(config.KNOWLEDGE_CANONICAL.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def render_knowledge(conn: sqlite3.Connection, *, per_capability: int = 6) -> str:
    """生成 where-things-are.md 的内容。

    这个文件的价值在于**人和任意会话都能直接读**，不需要查询层。索引里的能力标签
    回答「谁实现过飞书鉴权」，而这一点是 grep 做不到的——grep 要求你已经知道
    该搜 `tenant_access_token` 这个字符串。
    """
    canonical = _load_canonical()
    caps = conn.execute(
        """
        SELECT c.capability, COUNT(*) n, COUNT(DISTINCT f.project) p
        FROM capabilities c JOIN files f ON f.file_id = c.file_id
        GROUP BY c.capability ORDER BY n DESC
        """
    ).fetchall()

    totals = conn.execute(
        "SELECT COUNT(*) files, SUM(lines) lines, COUNT(DISTINCT project) projects FROM files"
    ).fetchone()

    lines: list[str] = [
        "# 已有实现在哪",
        "",
        "**此文件由 `sessionmcp.cli code index` 自动生成，不要手改**——每次刷新会覆盖。",
        "要标注「哪个是规范实现」，编辑 `~/.claude/knowledge/canonical.json`，",
        "格式 `{\"能力名\": \"路径\"}`，生成器只读不写它。",
        "",
        f"扫描范围：{totals['projects']} 个活跃项目、{totals['files']} 个代码文件、"
        f"约 {(totals['lines'] or 0) // 1000}k 行。",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 为什么需要这个文件",
        "",
        "同一个能力被反复重写，因为写的时候不知道别处已经有了。grep 只在**已经知道",
        "该搜什么字符串**时有用（查飞书鉴权得先知道关键词是 `tenant_access_token`）。",
        "下面按外部服务归类，不需要知道关键词。",
        "",
        "## 各能力的实现分布",
        "",
    ]

    unclaimed: list[str] = []
    for cap in caps:
        name = cap["capability"]
        lines.append(f"### {name} —— {cap['n']} 个文件 / {cap['p']} 个项目")
        lines.append("")
        if name in canonical:
            lines.append(f"**规范实现：`{canonical[name]}`** —— 优先复用这个。")
        else:
            lines.append("**规范实现：未指定**（在 canonical.json 里认领）")
            unclaimed.append(name)
        lines.append("")

        rows = conn.execute(
            """
            SELECT f.path, f.project, f.lines FROM capabilities c
            JOIN files f ON f.file_id = c.file_id
            WHERE c.capability = ? ORDER BY f.lines DESC LIMIT ?
            """,
            (name, per_capability),
        ).fetchall()
        for row in rows:
            lines.append(f"- `{_rel(row['path'])}` （{row['lines']} 行）")
        if cap["n"] > len(rows):
            lines.append(f"- …另有 {cap['n'] - len(rows)} 个，用 `code find --capability {name}` 看全部")
        lines.append("")

    if unclaimed:
        lines += [
            "## 待认领",
            "",
            "以下能力有多处实现但没指定规范实现。认领后 Agent 才知道该复用哪个，",
            "否则它只能看到一堆候选、大概率再抄一遍：",
            "",
            "".join(f"`{n}` " for n in unclaimed),
            "",
        ]

    forks = [f for f in detect_forks(conn) if f["likely_fork"]]
    if forks:
        lines += [
            "## ⚠️ 先看这个：同一代码库存在多份",
            "",
            "下面这些「项目」共享大量同名文件，是同一个代码库的多份拷贝并已分叉。",
            "**上面的重复数字因此虚高**——同一段代码在两份拷贝里各算一次。",
            "",
            "但两种情况长得一样，索引分不出来，需要人判断：",
            "",
            "- **意外拷贝**（换目录重开、忘了原来那份）→ 选一份保留，归档另一份",
            "- **刻意并行部署**（同一个 bot 服务两个不同的 Discord 服务器）→ 正常，",
            "  但要留意改动是否需要同步到两边",
            "",
        ]
        for f in forks:
            lines.append(
                f"- `{f['project_a']}` （{f['files_a']} 文件） ↔ "
                f"`{f['project_b']}` （{f['files_b']} 文件）"
            )
            lines.append(
                f"  同名文件 {f['shared_paths']} 个，其中 {f['same_line_count']} 个行数一致"
            )
        lines.append("")

    dup = list_duplication(conn)
    cross = [s for s in dup["cross_project_symbols"] if s["projects"] >= 2][:15]
    if cross:
        lines += [
            "## 跨项目同名函数（复制粘贴的直接证据）",
            "",
            "| 函数名 | 定义处数 | 跨项目数 |",
            "|---|---|---|",
        ]
        for item in cross:
            lines.append(f"| `{item['name']}` | {item['definitions']} | {item['projects']} |")
        lines.append("")

    return "\n".join(lines)


def write_knowledge(conn: sqlite3.Connection) -> Path:
    """把知识文件写到磁盘，返回路径。"""
    config.KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    config.KNOWLEDGE_MAP.write_text(render_knowledge(conn), encoding="utf-8")
    if not config.KNOWLEDGE_CANONICAL.exists():
        config.KNOWLEDGE_CANONICAL.write_text(
            '{\n  "_说明": "格式 {\\"能力名\\": \\"规范实现路径\\"}，删掉这行再填",\n'
            '  "_例": "feishu: ~/my-project/lib/feishu_auth.py"\n}\n',
            encoding="utf-8",
        )
    return config.KNOWLEDGE_MAP


def detect_forks(conn: sqlite3.Connection, *, min_shared: int = 20) -> list[dict[str, Any]]:
    """找出彼此是副本的项目对。

    一个项目被复制成两份（换个 remote、开个实验分支）之后，两边会有大量同名且
    内容一致的文件。那不是「两个项目重复实现了同一个能力」，是**一个项目存在两份
    并且已经分叉**。两者的修法完全不同：前者抽共享库，后者选一份归档另一份。
    不区分的话，重复统计会被严重高估，工作清单也会指错方向。
    """
    rows = conn.execute("SELECT path, project, lines FROM files").fetchall()
    by_project: dict[str, dict[str, int]] = {}
    for row in rows:
        # 去掉项目前缀，只留项目内相对路径
        rel = row["path"].replace(os.path.expanduser("~"), "~")
        prefix = row["project"]
        inner = rel[len(prefix):].lstrip("/") if rel.startswith(prefix) else rel
        by_project.setdefault(prefix, {})[inner] = row["lines"]

    out: list[dict[str, Any]] = []
    names = sorted(by_project)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = set(by_project[a]) & set(by_project[b])
            if len(shared) < min_shared:
                continue
            identical = sum(1 for k in shared if by_project[a][k] == by_project[b][k])
            out.append(
                {
                    "project_a": a,
                    "project_b": b,
                    "files_a": len(by_project[a]),
                    "files_b": len(by_project[b]),
                    "shared_paths": len(shared),
                    "same_line_count": identical,
                    # 行数相同不等于内容相同，但作为副本信号足够强
                    "likely_fork": identical / len(shared) > 0.4,
                }
            )
    out.sort(key=lambda x: -x["shared_paths"])
    return out


# 标准库和构建产物不算「已引入的第三方库」，混进来会让 top_imports 变成噪音
_STDLIB_NOISE = frozenset(
    {
        "__future__", "os", "sys", "re", "json", "time", "typing", "pathlib", "datetime",
        "logging", "asyncio", "collections", "dataclasses", "subprocess", "random",
        "hashlib", "math", "shutil", "traceback", "itertools", "functools", "enum",
        "csv", "io", "base64", "uuid", "copy", "string", "glob", "tempfile", "warnings",
        "argparse", "sqlite3", "urllib", "socket", "ssl", "threading", "contextlib",
        "abc", "textwrap", "unicodedata", "difflib", "email", "smtplib", "imaplib",
        "react", "next",
    }
)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) files, SUM(lines) lines, COUNT(DISTINCT project) projects FROM files"
    ).fetchone()
    return {
        "files": row["files"] or 0,
        "lines": row["lines"] or 0,
        "projects": row["projects"] or 0,
        "symbols": conn.execute("SELECT COUNT(*) n FROM symbols").fetchone()["n"],
        "distinct_symbols": conn.execute(
            "SELECT COUNT(*) n FROM (SELECT DISTINCT name FROM symbols)"
        ).fetchone()["n"],
        "capability_tags": conn.execute(
            "SELECT COUNT(*) n FROM capabilities"
        ).fetchone()["n"],
        "top_imports": {
            r["module"]: r["n"]
            for r in conn.execute(
                f"""
                SELECT module, COUNT(*) n FROM imports
                WHERE module NOT IN ({','.join('?' * len(_STDLIB_NOISE))})
                GROUP BY module ORDER BY n DESC LIMIT 15
                """,
                list(_STDLIB_NOISE),
            )
        },
    }
