# session-knowledge

[![Tests](https://github.com/nameforjt-afk/session-knowledge/actions/workflows/tests.yml/badge.svg)](https://github.com/nameforjt-afk/session-knowledge/actions/workflows/tests.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Claude Code's memory is scoped per project directory. Your history isn't.**

Every session you've ever run is already on disk in `~/.claude/projects/**/*.jsonl` —
every decision, every command that worked, every credential you pasted. But open a
session in a different directory and none of it is reachable. You re-ask questions you
already answered, and re-implement integrations you already wrote.

This indexes all of it and hands Claude 13 MCP tools to search it. You just ask normally.

> *What was the retry logic in that deploy script?*
> *Which DATABASE_URL did I actually use for staging?*
> *Before I write the Stripe integration — has anyone here already done payment auth?*

Pure Python standard library. No third-party dependencies, no embeddings, no API calls,
nothing leaves your machine.

```bash
git clone https://github.com/nameforjt-afk/session-knowledge.git
cd session-knowledge
bash install.sh
```

Restart Claude Code. Done.

---

## ⚠️ Read this before installing

The index lives in `~/.claude/session-index/` and **it contains plaintext credentials
scraped from your sessions.** That is the precondition for "which key did I use here"
to work at all.

**Never commit that directory, never sync it to cloud storage, never send it to anyone.**
This repo's `.gitignore` covers the default location, but if you relocate it that's on you.

The full-text index itself is redacted: common credential shapes (API keys, Bearer
tokens, JWTs, GitHub PATs, passwords inside connection strings) are replaced with
`⟦SECRET:fingerprint⟧` before they are written. Plaintext lives only in `vault.db`
at mode 0600, and only an explicit `creds get` returns it.

The transcript and code indexes still contain sensitive local context even after
redaction. Every generated SQLite database and WAL/SHM sidecar is restricted to mode
0600, but the index directory should still be treated as private data.

Redaction is pattern matching, not magic. Custom-format secrets can slip through.
`verify-redaction` spot-checks for leaks.

---

## What it actually solves

Four things, in rough order of how often they save you:

**1. "Have I already built this?"** — Search by *external service*, not by function name.
`grep` only works if you already know the string to search for (you need to know it's
`tenant_access_token` before you can grep for Feishu auth). Capability tags don't
require that.

**2. "How did we decide this?"** — The rationale is buried in thousands of past
instructions. Full-text search over them, filterable to just your own instructions.

**3. "How did that command go again?"** — Every Bash and MCP call, with arguments and
results. Filter to only the ones that *failed*, to recall the traps.

**4. "Which value does this key take?"** — Same variable name routinely has several
real values (multiple apps, dev vs prod, two tables). It returns all candidates ranked,
with the call site each came from, rather than silently picking one.

---

## Usage

### Day to day: nothing

A hook refreshes the index incrementally on each Claude Code start (async, non-blocking,
usually 1–2s). Ask questions normally; Claude reaches for the tools on its own.

Deleting a transcript removes its searchable text, tool calls, metadata, and session-only
credential observations on the next refresh. Credentials that still occur in another live
transcript remain available.

### CLI

```bash
python3 -m sessionmcp.cli index                     # incremental refresh
python3 -m sessionmcp.cli stats                     # index health check

python3 -m sessionmcp.cli search "deploy timeout"   # full text (multi-word = AND)
python3 -m sessionmcp.cli search "pricing" --kind user_instruction
python3 -m sessionmcp.cli tool "docker build"       # past commands and API calls
python3 -m sessionmcp.cli tool "stripe" --errors    # only the ones that failed
python3 -m sessionmcp.cli timeline "that migration" # reconstruct how it unfolded
python3 -m sessionmcp.cli synth "rate limiting"     # cross-session material pack
python3 -m sessionmcp.cli evolution "auth design"   # how a decision changed over time

python3 -m sessionmcp.cli code find --capability stripe   # who implemented payment auth
python3 -m sessionmcp.cli code find send_message          # by symbol name
python3 -m sessionmcp.cli code dup                        # duplicate-implementation report

python3 -m sessionmcp.cli creds list                 # variable names only, never values
python3 -m sessionmcp.cli creds get DATABASE_URL     # all candidates, ranked
python3 -m sessionmcp.cli creds get X --masked       # masked form only
python3 -m sessionmcp.cli verify-redaction           # confirm no plaintext in the index
```

**Search is AND. More words means fewer hits.** Use the 2–3 words you would actually
have typed back then, not a sentence.

### The fake-credential problem

Not every "credential" in your history is real. Doc examples, error snippets, values
someone made up mid-discussion — all get extracted, and they look exactly like the real
thing. A fake in your candidate list is worse than no record at all: it looks perfectly
plausible, so you use it.

Blacklist it permanently:

```bash
python3 -m sessionmcp.cli creds forget --fingerprint <fp> --reason "doc example"
```

Deleting alone does nothing — the index re-scans daily and the value comes right back.
`forget` is delete **plus** blacklist.

---

## The 13 MCP tools

| | |
|---|---|
| `search_sessions` | full-text search across every session |
| `get_session` | page through one session's full transcript |
| `list_sessions` | session metadata (title, project, date, turns) |
| `get_timeline` | chronological reconstruction of one topic |
| `synthesize_topic` | curated excerpts across sessions, for summarizing |
| `track_evolution` | how a decision or standard changed, bucketed by month |
| `find_tool_call` | past Bash/MCP calls with arguments; `errors_only` to recall traps |
| `find_implementation` | existing code by symbol **or by external-service tag** |
| `list_duplication` | what got implemented N times, and which "projects" are forks |
| `list_credentials` | variable-name registry — never returns values |
| `get_credential` | the actual values, all candidates ranked by trustworthiness |
| `lookup_secret` | resolve a `⟦SECRET:fp⟧` fingerprint back to its variable name |
| `index_stats` | coverage and freshness |

---

## How it works

```
~/.claude/projects/**/*.jsonl      transcripts Claude Code already writes
            ↓  single pass
   ┌────────┴────────┐
 index side        credential side
 redacted → FTS5   KEY=VALUE → vault.db (0600)
   ↓                  ↓
index.db          vault.db          code.db
full text         credential        symbols +
                  registry          capability tags
```

CJK search uses bigram pre-tokenization. FTS5's built-in trigram cannot match two-character
Chinese words (measured: common two-char terms returned nothing), and `unicode61` doesn't
segment CJK at all. So CJK text is expanded into adjacent character pairs at write time and
the query is expanded the same way — exact matches work, still on the FTS5 index, BM25
ranking preserved.

---

## Configuration

Most of it needs none. To tune, edit `sessionmcp/config.py`:

| Setting | What it does |
|---|---|
| `CODE_CAPABILITY_PATTERNS` | **The one worth editing.** Service tags that drive "find existing implementations by service". Drop the ones you don't use, add yours |
| `PRIVATE_TITLES` | Session titles to exclude from the index, exact match |
| `_PRIVATE_KEYWORDS` | Heuristic for personal content; sessions that hit enough of these are skipped |
| `CODE_PROJECT_*` | Which directories the code index scans (auto-derived by default) |

### Which directories get code-indexed

Auto-derived: working directories with **≥2 sessions** in the last 45 days, top 30 by
session count. Not hard-coding a list is deliberate — a hard-coded list goes stale
silently as projects come and go, and is entirely wrong on a new machine.

If you're new to Claude Code and every project has exactly one session, this derives an
empty list and the code index scans nothing. Override:

```bash
export SESSION_KNOWLEDGE_PROJECT_DIRS="~/proj-a:~/proj-b"
```

### Claiming a canonical implementation

`~/.claude/knowledge/canonical.json` lets you declare which file is the canonical
implementation of a capability. The generator reads it and never overwrites it, so daily
refreshes won't clobber your call. When the same logic shows up a third time, claiming
one beats copying it a fourth.

---

## Uninstall

```bash
bash uninstall.sh            # remove MCP + hook, keep the index
bash uninstall.sh --purge    # remove the index too
```

---

## Known limits

- **Claude Code transcripts only** (`~/.claude/projects/**/*.jsonl`). Not Cursor, not Copilot.
- **Redaction is pattern matching.** Common shapes are covered; custom formats can leak.
  Run `verify-redaction` periodically.
- **Code index reads Python/JS/TS only** (`.py .js .ts .tsx .mjs .jsx`).
- **First full index** of a few hundred sessions takes 1–3 minutes. Incremental after that is 1–2s.
- **Python 3.10+**, with FTS5 compiled into the bundled sqlite3. The system Python on macOS
  sometimes lacks it — install from python.org or `brew install python`.

[中文文档](README.zh-CN.md)

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or pull request. Never
include real transcripts or credentials in public reports. Potential vulnerabilities
should be reported privately according to [SECURITY.md](SECURITY.md).

Release history is recorded in [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
