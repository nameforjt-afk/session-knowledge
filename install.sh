#!/bin/bash
# session-knowledge 一键安装：注册 MCP server + SessionStart hook + 建首次索引。
# 重复运行是安全的，不会重复添加 hook，也不会动你已有的其它配置。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"
CLAUDE_JSON="$HOME/.claude.json"
DATA="$CLAUDE_DIR/session-index"

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. 环境自检

say "[1/5] 检查运行环境"

PYTHON="$(command -v python3 2>/dev/null || true)"
[ -n "$PYTHON" ] || die "找不到 python3。装一个再来：https://www.python.org/downloads/"

"$PYTHON" - <<'PY' || die "环境不满足，见上面的报错"
import sys, sqlite3
if sys.version_info < (3, 10):
    sys.exit(f"需要 Python 3.10+，当前是 {sys.version.split()[0]}")
try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
except sqlite3.OperationalError:
    sys.exit("你的 Python 自带的 sqlite3 没编译 FTS5 全文检索，本工具依赖它。\n"
             "  macOS 建议装 python.org 官方版或 `brew install python`。")
PY
ok "$("$PYTHON" -V) · sqlite3 FTS5 可用 · 零第三方依赖"

[ -d "$CLAUDE_DIR/projects" ] || die "没找到 $CLAUDE_DIR/projects —— 这台机器还没用过 Claude Code？"
JSONL_COUNT=$(find "$CLAUDE_DIR/projects" -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')
[ "$JSONL_COUNT" -gt 0 ] || die "$CLAUDE_DIR/projects 里没有 session 文件，没什么可索引的"
ok "发现 $JSONL_COUNT 个 session 文件待索引"

# hook 的 PATH 可能很窄，把绝对路径记下来给 refresh.sh 用
echo "$PYTHON" > "$ROOT/.python-path"
chmod +x "$ROOT/refresh.sh"

# ---------------------------------------------------------------- 2. 注册 MCP

say "[2/5] 注册 MCP server（写入 ~/.claude.json）"

"$PYTHON" - "$CLAUDE_JSON" "$ROOT" "$PYTHON" <<'PY'
import json, os, shutil, sys

path, root, python = sys.argv[1], sys.argv[2], sys.argv[3]

data = {}
if os.path.exists(path):
    shutil.copy2(path, path + ".bak")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

servers = data.setdefault("mcpServers", {})
existed = "session-knowledge" in servers
servers["session-knowledge"] = {
    "type": "stdio",
    "command": python,
    "args": ["-m", "sessionmcp.server"],
    "env": {"PYTHONPATH": root},
}

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
print(f"  \033[1;32m✓ {'更新' if existed else '新增'} mcpServers['session-knowledge']"
      f"{'（旧配置已备份为 .claude.json.bak）' if existed else ''}\033[0m")
PY

# ---------------------------------------------------------------- 3. 注册 hook

say "[3/5] 注册 SessionStart hook（每次开会话自动增量刷新索引）"

"$PYTHON" - "$SETTINGS" "$ROOT/refresh.sh" <<'PY'
import json, os, shutil, sys

path, script = sys.argv[1], sys.argv[2]

data = {}
if os.path.exists(path):
    shutil.copy2(path, path + ".bak")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

entry = {"type": "command", "command": script, "timeout": 120, "async": True}
groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])

# 已经装过就别再加一遍。判重看 command，因为用户可能手改过 timeout。
for group in groups:
    for hook in group.get("hooks", []):
        if hook.get("command") == script:
            hook.update(entry)
            break
    else:
        continue
    break
else:
    groups.append({"hooks": [entry]})

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
print("  \033[1;32m✓ hooks.SessionStart 已就位（不影响你已有的其它 hook）\033[0m")
PY

# ---------------------------------------------------------------- 4. 首次索引

say "[4/5] 建立索引（第一次要全量扫，几百个 session 大约 1-3 分钟）"

mkdir -p "$DATA"
cd "$ROOT"
"$PYTHON" -m sessionmcp.cli index
"$PYTHON" -m sessionmcp.cli scan-env  2>/dev/null | tail -2 || warn ".env 扫描跳过（不影响检索）"
"$PYTHON" -m sessionmcp.cli code index 2>/dev/null | tail -2 || warn "代码索引跳过（不影响检索）"

# ---------------------------------------------------------------- 5. 自检

say "[5/5] 自检"
"$PYTHON" -m sessionmcp.cli stats

cat <<EOF

$(printf '\033[1;32m装好了。\033[0m')

  下一步：重启 Claude Code，然后随便问一句
    "帮我查一下历史 session 里关于 XXX 的讨论"

  命令行也能直接用：
    cd $ROOT
    python3 -m sessionmcp.cli search "关键词"

$(printf '\033[1;33m⚠ 一件要记住的事\033[0m')
  索引库在 $DATA，里面含有从你 session 里扫出来的
  凭证明文（这是"查一下当时用的哪个 key"能成立的前提）。
  这个目录永远不要提交到 git、不要同步到网盘。
  本仓库的 .gitignore 已经挡住了它，但换个位置就得自己注意。

  卸载：bash $ROOT/uninstall.sh
EOF
