#!/bin/bash
# 卸载：摘掉 MCP 注册和 SessionStart hook。索引库默认保留，加 --purge 才删。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="$HOME/.claude/session-index"
PURGE="${1:-}"

PYTHON="$(command -v python3)"

"$PYTHON" - "$HOME/.claude.json" "$ROOT" <<'PY'
import json, os, sys
path, root = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
from sessionmcp.configio import atomic_write_json

if not os.path.exists(path):
    sys.exit(0)
with open(path, encoding="utf-8") as f:
    data = json.load(f)
if data.get("mcpServers", {}).pop("session-knowledge", None) is None:
    print("  MCP 注册本来就不在")
else:
    atomic_write_json(path, data)
    print("  ✓ 已摘掉 MCP 注册")
PY

"$PYTHON" - "$HOME/.claude/settings.json" "$ROOT/refresh.sh" "$ROOT" <<'PY'
import json, os, sys
path, script, root = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
from sessionmcp.configio import atomic_write_json

if not os.path.exists(path):
    sys.exit(0)
with open(path, encoding="utf-8") as f:
    data = json.load(f)

groups = data.get("hooks", {}).get("SessionStart", [])
before = sum(len(g.get("hooks", [])) for g in groups)
for g in groups:
    g["hooks"] = [h for h in g.get("hooks", []) if h.get("command") != script]
groups[:] = [g for g in groups if g.get("hooks")]
after = sum(len(g.get("hooks", [])) for g in groups)

if before == after:
    print("  hook 本来就不在")
else:
    if not groups:
        data["hooks"].pop("SessionStart", None)
    if not data.get("hooks"):
        data.pop("hooks", None)
    atomic_write_json(path, data)
    print("  ✓ 已摘掉 SessionStart hook")
PY

if [ "$PURGE" = "--purge" ]; then
    rm -rf "$DATA"
    echo "  ✓ 索引库已删除（${DATA}）"
else
    echo "  索引库保留在 ${DATA}，要一并删除请跑：bash uninstall.sh --purge"
fi

echo "卸载完成。重启 Claude Code 生效。"
