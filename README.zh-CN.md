# session-knowledge

[English](README.md) · 中文

[![Tests](https://github.com/nameforjt-afk/session-knowledge/actions/workflows/tests.yml/badge.svg)](https://github.com/nameforjt-afk/session-knowledge/actions/workflows/tests.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

把你积累的 Claude Code 历史会话变成可检索的知识库，让任何一个新会话都能查到
「这事之前是怎么定的」「那条命令当时怎么跑的」「这个 key 用的哪个值」。

装完之后 Claude 会多出 13 个 MCP 工具，你正常提问它自己就会去查。

```bash
git clone https://github.com/nameforjt-afk/session-knowledge.git
cd session-knowledge
bash install.sh
```

零第三方依赖（纯 Python 标准库），装完重启 Claude Code 即可。

---

## ⚠️ 先看这条

索引库建在 `~/.claude/session-index/`，**里面有从你会话里扫出来的凭证明文**——
这是「查一下当时用的哪个 key」这个功能能成立的前提。

**这个目录永远不要提交 git、不要同步网盘、不要打包发给别人。**
本仓库 `.gitignore` 已经挡住了默认位置，但你要是改了路径就得自己注意。

全文索引本身是脱敏的：常见凭证格式（API key、Bearer token、JWT、GitHub PAT、
连接串里的密码）在入库前会被替换成 `⟦SECRET:指纹⟧`，明文只存在权限 0600 的
`vault.db` 里，要显式调 `creds get` 才拿得到。

全文索引和代码索引即使完成脱敏，仍包含敏感的本地上下文。所有生成的 SQLite 数据库
及其 WAL/SHM 临时文件都会限制为 0600，但整个索引目录仍应当作私密数据管理。

但脱敏是模式匹配，不是万能——自定义格式的密钥可能漏网。

---

## 它解决什么

Claude Code 的自动记忆是**按项目目录隔离**的：换个目录开会话，别处存的记忆一条
都看不到。而真正想找的东西恰恰是跨项目的：

- **当时怎么拍板的** —— 散在几千条历史指令里的口径和决策
- **跑通过的操作** —— 每一次 Bash 调用、MCP 调用，连同参数和返回
- **凭证到底用哪个** —— 同一个变量名往往有多个取值（多个应用、多张表、开发/生产混用）
- **这个能力是不是已经实现过了** —— 按外部服务标签查，而不是靠猜函数名

最后一条是最省事的：写新集成之前先问一句「有人实现过飞书鉴权吗」，
比 grep 有效得多——grep 要求你已经知道该搜 `tenant_access_token`，
按服务标签查不需要。

---

## 用法

### 平时：什么都不用做

装好后每次开 Claude Code 会自动增量刷新索引（异步，不阻塞启动，通常 1-2 秒）。
你直接提问就行：

删除原始会话文件后，下次刷新会同步清理它的正文、工具调用、元数据和仅来自该会话的
凭证观测；如果同一凭证仍存在于其他未删除会话中，则会继续保留。

> 上次那个部署脚本的重试逻辑是怎么写的？
> 这个 DATABASE_URL 我之前用的哪个值？
> 写 Stripe 对接之前先查查有没有现成的

### 命令行

```bash
cd session-knowledge

python3 -m sessionmcp.cli index                    # 增量刷新索引
python3 -m sessionmcp.cli stats                    # 索引概况自检

python3 -m sessionmcp.cli search "部署 超时"        # 全文检索（多词是 AND）
python3 -m sessionmcp.cli search "口径" --kind user_instruction
python3 -m sessionmcp.cli tool "docker build"      # 翻历史命令和 API 调用
python3 -m sessionmcp.cli tool "feishu" --errors   # 只看失败过的，回忆踩过的坑
python3 -m sessionmcp.cli timeline "这个方案"        # 按时间还原一件事的推进
python3 -m sessionmcp.cli synth "封号"              # 跨会话收集素材包供归纳
python3 -m sessionmcp.cli evolution "定价标准"       # 主题演进：改了几版、为什么

python3 -m sessionmcp.cli code find --capability feishu   # 谁实现过飞书鉴权
python3 -m sessionmcp.cli code find send_message          # 按函数名查
python3 -m sessionmcp.cli code dup                        # 重复实现清单

python3 -m sessionmcp.cli creds list                # 变量名录（不返回值）
python3 -m sessionmcp.cli creds get DATABASE_URL    # 取值，返回全部候选
python3 -m sessionmcp.cli creds get X --masked      # 只看遮蔽形式
python3 -m sessionmcp.cli verify-redaction          # 确认索引里没有明文密钥
```

**检索是 AND 语义，词越多命中越少。** 查历史用 2-3 个当时真会用的词，不要写长句。

### 关于假凭证

会话里出现的「凭证」不全是真的——文档示例、报错片段、讨论时随手编的样例值
都会被抽进来，而且长得跟真值一模一样。一个混进候选列表的假值比没有这条记录
更危险：它看起来完全合理，你会拿去用。

发现假值就永久拉黑：

```bash
python3 -m sessionmcp.cli creds forget --fingerprint <fp> --reason "文档示例"
```

只删是没用的——索引每天自动重扫，两秒后同样的值又回来了。`forget` 是删除 + 拉黑。

---

## 工作原理

```
~/.claude/projects/**/*.jsonl        Claude Code 自己写的会话记录
            ↓  单遍解析
   ┌────────┴────────┐
索引侧             凭证侧
脱敏后入 FTS5      KEY=VALUE 抽进 vault.db (0600)
   ↓                  ↓
index.db          vault.db          code.db
全文检索           凭证登记表         代码符号 + 能力标签
```

中文检索用 bigram 预分词。FTS5 自带的 trigram 对两字中文词无法匹配（实测「部署」
「打标」全部返回空），unicode61 则完全不切分。所以写入前把中文展开成相邻二字组，
查询时做同样展开——两字词能精确命中，且仍走 FTS5 索引，保留 BM25 排序。

---

## 配置

大部分东西不用配。要调就改 `sessionmcp/config.py`：

| 项 | 说明 |
|---|---|
| `CODE_CAPABILITY_PATTERNS` | **最值得改的一个。** 外部服务标签，决定「按服务查已有实现」认得哪些服务。删掉用不上的，加上你在用的 |
| `PRIVATE_TITLES` | 明确不想进索引的会话标题，精确匹配 |
| `_PRIVATE_KEYWORDS` | 私人内容启发式。命中够多且压过工作词的会话会被跳过 |
| `CODE_PROJECT_*` | 代码索引范围。默认自动推导，见下 |

### 代码索引扫哪些目录

默认自动推导：从会话记录里取最近 45 天开过 **≥2 次**会话的工作目录，按会话数
排序取前 30 个。不写死清单是刻意的——写死会随项目增删悄悄过期，换台机器全错。

刚开始用 Claude Code 不久、每个项目只开过一次会话的话，这里会推导出空清单，
代码索引就什么都不扫。手动指定：

```bash
export SESSION_KNOWLEDGE_PROJECT_DIRS="~/proj-a:~/proj-b"
```

### 认领「规范实现」

`~/.claude/knowledge/canonical.json` 里可以手写「哪个文件是某个能力的规范实现」，
生成器只读不写，不会被每日刷新冲掉。同一段逻辑第三次出现时，
认领一份规范实现比抄第四遍强。

---

## 卸载

```bash
bash uninstall.sh            # 摘掉 MCP 和 hook，索引保留
bash uninstall.sh --purge    # 连索引库一起删
```

---

## 已知边界

- **只认 Claude Code 的会话记录**（`~/.claude/projects/**/*.jsonl`）。Cursor、
  Copilot 那些不在范围内。
- **脱敏是模式匹配**，覆盖常见格式，自定义格式的密钥可能漏网。定期跑
  `verify-redaction` 抽查。
- **代码索引只认 Python/JS/TS**（`.py .js .ts .tsx .mjs .jsx`）。
- **首次全量索引**几百个会话大约 1-3 分钟，之后增量刷新 1-2 秒。
- 需要 **Python 3.10+**，且自带的 sqlite3 要编译了 FTS5。macOS 系统自带的
  Python 有时不满足，装 python.org 官方版或 `brew install python` 即可。

## 参与贡献与安全报告

提交 Issue 或 PR 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。公开报告中不要放入
真实会话或凭证；潜在漏洞请按 [SECURITY.md](SECURITY.md) 通过私密渠道报告。

版本变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## License

MIT，见 [LICENSE](LICENSE)。
