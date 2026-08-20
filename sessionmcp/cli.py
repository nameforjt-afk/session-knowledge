"""命令行入口。

    python3 -m sessionmcp.cli index          建/更新索引
    python3 -m sessionmcp.cli search 部署流程   检索
    python3 -m sessionmcp.cli creds get DATABASE_URL

索引是单遍解析同时喂全文索引和凭证库——800MB 扫两次没有必要。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import query as q
from . import vault as v
from .indexer import IndexWriter, connect as connect_index
from pathlib import Path

from .parse import iter_session_files, iter_subagent_files, parse_session
from .vault import VaultWriter, connect as connect_vault


def _dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 建索引

def cmd_index(args: argparse.Namespace) -> int:
    index_conn = connect_index()
    vault_conn = connect_vault()
    index_writer = IndexWriter(index_conn)
    vault_writer = VaultWriter(vault_conn)

    # (路径, 父 session id)；主 session 的父 id 为空
    files: list[tuple[Path, str]] = [(p, "") for p in iter_session_files()]
    if not args.no_subagents:
        files.extend(iter_subagent_files())

    started = time.time()
    done = skipped = empty = subagents = 0
    credentials = 0

    for position, (path, parent) in enumerate(files, 1):
        if not args.force and not index_writer.needs_reindex(path):
            skipped += 1
            continue

        parsed = parse_session(path, parent_session_id=parent)
        if parsed is None:
            # 只有元数据行、没有任何对话内容的文件。这不是解析失败——报成
            # "失败" 会让人以为有 bug 要查，实际上没有任何东西可索引。
            empty += 1
            continue

        index_writer.write(parsed)
        credentials += vault_writer.write(parsed)
        done += 1
        if parsed.is_subagent:
            subagents += 1

        if done % 10 == 0:
            index_conn.commit()
            vault_conn.commit()
        print(
            f"\r  [{position}/{len(files)}] 已入库 {done}  跳过 {skipped}  {path.stem[:8]}",
            end="",
            file=sys.stderr,
            flush=True,
        )

    index_conn.commit()
    vault_conn.commit()
    print(file=sys.stderr)

    if done and not args.no_optimize:
        print("  合并索引段…", file=sys.stderr)
        index_writer.optimize()

    elapsed = time.time() - started
    print(
        f"完成：入库 {done}（其中子 agent {subagents}），跳过 {skipped}，空文件 {empty}，"
        f"凭证观测 {credentials} 条，耗时 {elapsed:.1f}s"
    )
    return 0


# ---------------------------------------------------------------- 查询

def cmd_scan_env(args: argparse.Namespace) -> int:
    """扫描项目目录下的 .env，把当前真正在用的值接进凭证登记表。

    项目目录取自索引里记录的 cwd——session 去过的地方就是要扫的地方。
    """
    from .envscan import scan

    index_conn = connect_index()
    roots = [
        Path(row["cwd"])
        for row in index_conn.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE cwd != '' AND is_private = 0"
        )
    ]
    if args.path:
        roots = [Path(p).expanduser() for p in args.path]

    # 子目录已被父目录覆盖时不重复遍历
    roots = sorted({r for r in roots if r.is_dir()}, key=lambda p: len(str(p)))
    pruned: list[Path] = []
    for root in roots:
        if not any(str(root).startswith(f"{kept}/") for kept in pruned):
            pruned.append(root)

    vault_conn = connect_vault()
    writer = VaultWriter(vault_conn)
    writer.drop_env_rows()

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    total = 0
    files = 0
    for env_file, assignments in scan(pruned):
        real = sum(1 for a in assignments if not a.is_placeholder)
        total += writer.write_env(
            assignments,
            project=str(env_file.project_dir),
            source_path=str(env_file.path),
            stamp=stamp,
        )
        files += 1
        label = "模板" if env_file.is_template else "现值"
        print(f"  [{label}] {env_file.path}  {len(assignments)} 项（真实值 {real}）")

    vault_conn.commit()
    print(f"\n扫描 {len(pruned)} 个项目目录，{files} 个 .env 文件，登记 {total} 项")
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    """跨项目代码索引：写新东西之前先查有没有人写过。"""
    from . import codeindex as ci

    conn = ci.connect()

    if args.code_action == "index":
        writer = ci.CodeWriter(conn)
        files = list(ci.iter_code_files())
        started = time.time()
        done = skipped = 0
        for path, project in files:
            if not args.force and not writer.needs_reindex(path):
                skipped += 1
                continue
            parsed = ci.parse_file(path, project)
            if parsed is None:
                continue
            writer.write(parsed)
            done += 1
        pruned = writer.prune_missing()
        conn.commit()
        target = ci.write_knowledge(conn)
        print(
            f"完成：入库 {done}，跳过 {skipped}，清理已删除 {pruned}，"
            f"耗时 {time.time() - started:.1f}s"
        )
        print(f"知识文件已生成：{target}")
        return 0

    if args.code_action == "find":
        payload = ci.find_implementation(
            conn, args.query or "", capability=args.capability, project=args.project
        )
        if args.json:
            _dump(payload)
            return 0
        syms = payload["symbols"]
        if syms:
            print(f"符号命中 {len(syms)} 个：")
            for s in syms:
                owner = f"{s['parent']}." if s["parent"] else ""
                print(f"  {owner}{s['name']}{s['signature']}  [{s['kind']}]")
                print(f"      {s['location']}")
        for group in payload["by_capability"]:
            print(f"\n能力「{group['capability']}」现有 {group['file_count']} 处实现：")
            for f in group["files"]:
                print(f"  {f['location']}  （{f['lines']} 行）")
        if not syms and not payload["by_capability"]:
            print("无命中——可以认为没有已有实现")
        return 0

    if args.code_action == "dup":
        payload = ci.list_duplication(conn, min_count=args.min_count)
        if args.json:
            _dump(payload)
            return 0
        print(f"{'能力':20s} {'文件数':>6s} {'项目数':>6s}")
        print("-" * 36)
        for c in payload["by_capability"]:
            print(f"{c['capability']:20s} {c['files']:6d} {c['projects']:6d}")
        print(f"\n跨项目同名函数（≥{args.min_count} 处定义、≥2 个项目）：")
        for s in payload["cross_project_symbols"][:20]:
            print(f"  {s['definitions']:3d}× 跨 {s['projects']} 项目  {s['name']}")
        return 0

    if args.code_action == "stats":
        payload = ci.stats(conn)
        if args.json:
            _dump(payload)
            return 0
        print(f"代码文件      {payload['files']}")
        print(f"代码行数      {payload['lines']}")
        print(f"项目数        {payload['projects']}")
        print(f"符号定义      {payload['symbols']}（{payload['distinct_symbols']} 个不同名字）")
        print(f"能力标签      {payload['capability_tags']} 条")
        print("\n最常用的第三方库：")
        for mod, n in payload["top_imports"].items():
            print(f"  {mod:24s} {n}")
        return 0

    print("未知的 code 子命令")
    return 1


def cmd_stats(args: argparse.Namespace) -> int:
    payload = q.stats(connect_index())
    if args.json:
        _dump(payload)
        return 0

    print(f"session 总数        {payload['sessions']}（私人排除 {payload['private_excluded']}）")
    print(f"被压缩过的 session  {payload['compacted_sessions']}")
    print(f"compact 摘要记录    {payload['compact_records']}")
    print(f"user 指令记录       {payload['user_records']}")
    print(f"时间跨度            {payload['span']}")
    print(
        f"chunk 总数          {payload['total_chunks']}"
        f"（去重后 {payload['unique_chunks']}，副本率 {payload['duplicate_ratio']:.0%}）"
    )
    print("\n按类型：")
    for kind, count in sorted(payload["chunks_by_kind"].items(), key=lambda x: -x[1]):
        print(f"  {kind:20s} {count:8d}")
    print("\n工具调用 TOP：")
    for tool, count in payload["tool_calls"].items():
        print(f"  {tool:28s} {count:8d}")
    print("\n项目：")
    for item in payload["projects"][:10]:
        print(f"  {item['sessions']:4d}  {item['project']}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    hits = q.search(
        connect_index(),
        args.query,
        project=args.project,
        kind=args.kind,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )
    if args.json:
        _dump(hits)
        return 0

    if not hits:
        print("无命中")
        return 0
    for hit in hits:
        marker = " 🔑" if hit["has_secret_ref"] else ""
        copies = f" ×{hit['copies']}副本" if hit["copies"] > 1 else ""
        agent = " ⤷子agent" if hit["is_subagent"] else ""
        print(f"\n[{(hit['ts'] or '')[:10]}] {hit['session_id'][:8]}  {hit['kind']}{marker}{copies}{agent}")
        print(f"  {hit['title'][:70]}  ({hit['project'][-30:]})")
        print(f"  {hit['snippet']}")
    print(f"\n共 {len(hits)} 条")
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    payload = q.get_session(
        connect_index(), args.session_id, offset=args.offset, limit=args.limit, kind=args.kind
    )
    if args.json:
        _dump(payload)
        return 0
    if "error" in payload:
        print(payload["error"])
        return 1

    print(f"{payload['title']}  [{payload['project']}]")
    print(f"{payload['started_at'][:16]} → {payload['ended_at'][:16]}  分支={payload['git_branch']}")
    print(f"共 {payload['total_chunks']} 段，本页 {payload['returned']}（offset={payload['offset']}）")
    if payload["was_compacted"]:
        print(f"⚠ 此 session 被压缩过 {payload['compact_records']} 次，早期原文已丢失")
    for chunk in payload["chunks"]:
        print(f"\n--- #{chunk['seq']} {chunk['kind']} {(chunk['ts'] or '')[:16]} ---")
        print(chunk["text"][:1200])
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    rows = q.list_sessions(
        connect_index(), project=args.project, since=args.since, limit=args.limit
    )
    if args.json:
        _dump(rows)
        return 0
    print(f"{'日期':11s} {'id':9s} {'轮':>4s} {'工具':>6s}  标题")
    print("-" * 96)
    for row in rows:
        flag = "†" if row["was_compacted"] else " "
        print(
            f"{row['date']:11s} {row['session_id']:9s} {row['user_turns']:4d} "
            f"{row['tools']:6d}{flag} {row['title'][:52]}"
        )
    print(f"\n共 {len(rows)} 条    † = 被压缩过")
    return 0


def cmd_tool(args: argparse.Namespace) -> int:
    rows = q.find_tool_call(
        connect_index(),
        args.pattern,
        tool=args.tool,
        session=args.session,
        errors_only=args.errors,
        limit=args.limit,
    )
    if args.json:
        _dump(rows)
        return 0
    for row in rows:
        status = "✗" if row["failed"] else "✓"
        print(f"\n{status} [{row['date']}] {row['tool']}  {row['session_id']}  ({row['project'][-26:]})")
        print(f"  {row['target'][:300]}")
        if row["result_head"]:
            print(f"  → {row['result_head'][:160]}")
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    rows = q.get_timeline(connect_index(), args.topic, since=args.since, limit=args.limit)
    if args.json:
        _dump(rows)
        return 0
    for row in rows:
        print(f"\n{row['date']}  {row['session_id']}  命中{row['hits']}  {row['title'][:56]}")
        print(f"  {row['snippet'][:220]}")
    print(f"\n共 {len(rows)} 个时间点")
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    payload = q.synthesize_topic(
        connect_index(), args.query, max_sessions=args.max_sessions, since=args.since
    )
    if args.json:
        _dump(payload)
        return 0
    print(f"「{payload['query']}」跨 {payload['session_count']} 个 session，共 {payload.get('total_hits', 0)} 处命中\n")
    for bundle in payload["sessions"]:
        print(f"── {bundle['date']}  {bundle['title'][:56]}  ({bundle['total_hits']} 处)")
        for excerpt in bundle["excerpts"]:
            print(f"   [{excerpt['kind']}] {excerpt['text'][:200]}")
        print()
    return 0


def cmd_evolution(args: argparse.Namespace) -> int:
    payload = q.track_evolution(connect_index(), args.topic, since=args.since)
    if args.json:
        _dump(payload)
        return 0
    print(f"「{payload['topic']}」演进  {payload['span']}\n")
    for point in payload["points"]:
        print(f"{point['month']}  {point['sessions']} 个session / {point['hits']} 处命中  [{point['kind']}]")
        print(f"  {point['snippet'][:220]}\n")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    payload = q.verify_redaction(connect_index(), sample=args.sample)
    if args.json:
        _dump(payload)
        return 0
    print(f"扫描记录 {payload['scanned_records']}")
    if payload["clean"]:
        print("✓ 索引中无明文密钥残留")
        return 0
    print(f"✗ 发现 {payload['leak_count']} 处明文密钥")
    for label, count in sorted(payload["leaks_by_type"].items(), key=lambda x: -x[1]):
        print(f"  {label:28s} {count}")
    for example in payload["examples"]:
        print(f"  例: session {example['session_id']} → {example['label']}")
    return 1


# ---------------------------------------------------------------- 凭证

def cmd_creds(args: argparse.Namespace) -> int:
    conn = connect_vault()

    if args.creds_action == "list":
        rows = v.list_credentials(
            conn, service=args.service, key_name=args.name, include_placeholders=args.placeholders
        )
        if args.json:
            _dump(rows)
            return 0
        print(f"{'变量名':34s} {'服务':11s} {'类型':11s} {'取值数':>6s} {'次数':>6s} {'最近':11s}")
        print("-" * 92)
        for row in rows:
            flag = " ⚠冲突" if row["has_conflict"] else ""
            print(
                f"{row['key_name']:34s} {row['service']:11s} {row['kind']:11s} "
                f"{row['variants']:6d} {row['total_hits']:6d} {row['last_seen']:11s}{flag}"
            )
        print(f"\n共 {len(rows)} 个变量名。取值请用: creds get <变量名>")
        return 0

    if args.creds_action == "get":
        rows = v.get_credential(conn, args.name, project=args.project, reveal=not args.masked)
        if args.json:
            _dump(rows)
            return 0
        if not rows:
            print(f"未找到 {args.name}")
            return 1
        real = [r for r in rows if not r["is_placeholder"]]
        print(f"{args.name}：{len(rows)} 个候选（真实值 {len(real)}，占位符 {len(rows) - len(real)}）")
        if len(real) > 1:
            print("⚠ 存在多个真实取值，按可信度排序，请自行确认使用哪一个\n")
        for position, row in enumerate(rows, 1):
            tags = ["✅ .env 现值"] if row["source"] == "env_file" else ["session 历史"]
            if row["is_placeholder"]:
                tags.append("占位符")
            if row["is_local"]:
                tags.append("本地地址")
            shown = row["value"] if row["value"] is not None else row["masked"]
            print(f"{position}. {shown}  [{' / '.join(tags)}]")
            print(f"   服务={row['service']} 项目={row['project'][-40:]} 出现{row['occurrences']}次 最近={row['last_seen']}")
            if row["source_path"]:
                print(f"   来源: {row['source_path']}")
            elif row["usage_example"]:
                print(f"   用法: {row['usage_example'][:150]}")
            print()
        return 0

    if args.creds_action == "fp":
        rows = v.lookup_fingerprint(conn, args.fingerprint)
        if args.json:
            _dump(rows)
            return 0
        if not rows:
            print(f"指纹 {args.fingerprint} 无对应记录")
            return 1
        for row in rows:
            print(f"{row['key_name']}  服务={row['service']}  项目={row['project'][-30:]}  最近={row['last_seen'][:10]}")
            if row["usage_example"]:
                print(f"  用法: {row['usage_example'][:150]}")
        return 0

    if args.creds_action == "forget":
        removed = v.forget(
            conn,
            fingerprint=args.fingerprint or "",
            key_name=args.name or "",
            reason=args.reason or "",
        )
        target = args.fingerprint or args.name
        print(f"已删除 {removed} 条记录并永久拉黑（{target}）")
        print("该指纹以后不会再被抽取。撤销用: creds unblock <指纹>")
        return 0

    if args.creds_action == "blocked":
        rows = v.list_blocked(conn)
        if args.json:
            _dump(rows)
            return 0
        if not rows:
            print("黑名单为空")
            return 0
        for row in rows:
            print(f"  {row['value_fp']}  {row['key_name'] or '(未记名)':24s} {row['blocked_at'][:10]}  {row['reason']}")
        print(f"\n共 {len(rows)} 条")
        return 0

    if args.creds_action == "unblock":
        n = v.unblock(conn, args.fingerprint)
        print(f"已撤销拉黑 {n} 条。下次 index 后该值会重新进入登记表。")
        return 0

    if args.creds_action == "stats":
        _dump(v.stats(conn))
        return 0

    print("未知的 creds 子命令")
    return 1


# ---------------------------------------------------------------- 参数

def build_parser() -> argparse.ArgumentParser:
    # --json 放进共享父解析器，`search 部署 --json` 和 `--json search 部署` 都能用。
    # 只挂在主解析器上的话前者直接报错，而前者才是更自然的写法。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出原始 JSON")

    parser = argparse.ArgumentParser(
        prog="sessionmcp", description="Claude Code session 知识库", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)

    p = add("index", help="建立或增量更新索引")
    p.add_argument("--force", action="store_true", help="忽略 mtime 判定，强制全量重建")
    p.add_argument("--no-optimize", action="store_true", help="跳过索引段合并（调试用）")
    p.add_argument("--no-subagents", action="store_true", help="不索引子 agent 转录")
    p.set_defaults(func=cmd_index)

    p = add("scan-env", help="扫描项目 .env，登记当前真实凭证")
    p.add_argument("--path", action="append", help="限定扫描目录，可重复；默认取索引里的全部项目 cwd")
    p.set_defaults(func=cmd_scan_env)

    p = add("code", help="跨项目代码索引：查已有实现，防重复造轮子")
    code_sub = p.add_subparsers(dest="code_action", required=True)

    def add_code(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return code_sub.add_parser(name, parents=[common], **kwargs)

    c = add_code("index", help="扫描活跃项目并生成知识文件")
    c.add_argument("--force", action="store_true", help="忽略 mtime，强制全量重扫")

    c = add_code("find", help="查已有实现（符号名 + 能力标签）")
    c.add_argument("query", nargs="?", help="函数名或能力关键词")
    c.add_argument("--capability", help="直接按能力标签查，如 feishu")
    c.add_argument("--project", help="限定项目")

    c = add_code("dup", help="重复实现清单（收敛成共享库的工作清单）")
    c.add_argument("--min-count", type=int, default=3)

    add_code("stats", help="代码索引概况")
    p.set_defaults(func=cmd_code)

    p = add("stats", help="索引概况自检")
    p.set_defaults(func=cmd_stats)

    p = add("search", help="全文检索")
    p.add_argument("query")
    p.add_argument("--project")
    p.add_argument("--kind", choices=[
        "user_instruction", "compact_summary", "assistant_text", "tool_call", "tool_error"
    ])
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_search)

    p = add("session", help="读取单个 session")
    p.add_argument("session_id")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--kind")
    p.set_defaults(func=cmd_session)

    p = add("sessions", help="列出 session")
    p.add_argument("--project")
    p.add_argument("--since")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_sessions)

    p = add("tool", help="翻历史命令与 API 调用")
    p.add_argument("pattern")
    p.add_argument("--tool")
    p.add_argument("--session")
    p.add_argument("--errors", action="store_true", help="只看失败的调用")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_tool)

    p = add("timeline", help="按时间还原主题")
    p.add_argument("topic")
    p.add_argument("--since")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_timeline)

    p = add("synth", help="跨 session 归纳素材包")
    p.add_argument("query")
    p.add_argument("--max-sessions", type=int, default=8)
    p.add_argument("--since")
    p.set_defaults(func=cmd_synth)

    p = add("evolution", help="主题演进追踪")
    p.add_argument("topic")
    p.add_argument("--since")
    p.set_defaults(func=cmd_evolution)

    p = add("verify-redaction", help="确认索引无明文密钥")
    p.add_argument("--sample", type=int, help="只抽查前 N 条")
    p.set_defaults(func=cmd_verify)

    p = add("creds", help="凭证登记表")
    creds_sub = p.add_subparsers(dest="creds_action", required=True)

    def add_creds(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return creds_sub.add_parser(name, parents=[common], **kwargs)


    c = add_creds("list", help="名录（不返回值）")
    c.add_argument("--service")
    c.add_argument("--name")
    c.add_argument("--placeholders", action="store_true", help="连占位符一起列出")

    c = add_creds("get", help="取值（显式）")
    c.add_argument("name")
    c.add_argument("--project")
    c.add_argument("--masked", action="store_true", help="只看遮蔽形式")

    c = add_creds("fp", help="按指纹反查")
    c.add_argument("fingerprint")

    c = add_creds("forget", help="删除误收的凭证并永久拉黑（示例值、编造的样例）")
    c.add_argument("--fingerprint", help="按指纹删除单个取值")
    c.add_argument("--name", help="按变量名删除全部取值")
    c.add_argument("--reason", help="备注为什么删，便于日后回看")

    add_creds("blocked", help="查看黑名单")

    c = add_creds("unblock", help="撤销拉黑")
    c.add_argument("fingerprint")

    add_creds("stats", help="凭证库概况")
    p.set_defaults(func=cmd_creds)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
