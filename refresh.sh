#!/bin/bash
# session-knowledge 索引刷新。由 SessionStart hook 异步调用。
#
# 两条纪律：
#   1. 绝不向 stdout 写任何东西。SessionStart hook 的 stdout 会被注入模型上下文，
#      刷新日志混进去纯属污染。所有输出重定向到日志文件。
#   2. 绝不返回非零。hook 失败会打扰用户，而索引晚刷新一次没有任何后果。
#
# .env 扫描与代码索引单独节流到每天一次——它们不像 session 那样每分钟变。

set -u

# 脚本自己所在目录就是安装位置，不写死——装到哪儿都能跑。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="$HOME/.claude/session-index"
LOG="$DATA/refresh.log"
LOCK="$DATA/.refresh.lock"
ENV_STAMP="$DATA/.last-env-scan"

mkdir -p "$DATA"

# install.sh 会把检测到的 python3 绝对路径写进这个文件。hook 的 PATH 可能很窄，
# 光靠 command -v 有时找不到；但也不能写死版本号路径——Python 升级后会静默失效。
PYTHON=""
if [ -f "$ROOT/.python-path" ]; then
    PYTHON="$(cat "$ROOT/.python-path")"
fi
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 2>/dev/null)"
fi
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    echo "$(date '+%F %T') 找不到可用的 python3，跳过刷新" >> "$LOG"
    exit 0
fi

# 陈旧锁清理必须在抢锁之前。进程被 SIGKILL（合盖休眠、强制退出、系统重启）时
# trap 不会执行，锁目录会永久留下——之后每次刷新都静默跳过，索引再也不更新，而且
# 没有任何迹象。这是最糟的失败模式：看起来一切正常，实际早就停了。
if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null
fi

# mkdir 是原子的，用它当锁。同时开多个会话时只有第一个真正跑。
if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
    cd "$ROOT" || exit 0

    "$PYTHON" -m sessionmcp.cli index 2>&1

    # .env 与代码索引每天各扫一次。代码不像 session 那样每分钟变，每次开会话都全扫
    # 没必要；.env 改动更少。共用一个时间戳，一起节流。
    if [ ! -f "$ENV_STAMP" ] || [ -n "$(find "$ENV_STAMP" -mmin +1440 2>/dev/null)" ]; then
        echo "--- 距上次超过 24 小时，重扫 .env 与代码索引 ---"
        "$PYTHON" -m sessionmcp.cli scan-env 2>&1 | tail -3
        "$PYTHON" -m sessionmcp.cli code index 2>&1 | tail -2
        touch "$ENV_STAMP"
    fi
} >> "$LOG" 2>&1

# 日志超过 200KB 就只保留尾部，避免无限增长
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 204800 ]; then
    tail -c 102400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit 0
