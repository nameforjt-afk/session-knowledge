# Changelog

All notable changes to session-knowledge are documented here.

## [Unreleased]

### Fixed

- Preserve legitimate user instructions that begin with HTML, XML, or JSX markup while
  continuing to exclude known Claude Code system envelopes
  ([#24](https://github.com/nameforjt-afk/session-knowledge/issues/24)).

## [0.1.2] - 2026-09-21

### Fixed

- Remove indexed content, tool calls, metadata, and session-derived credential observations
  after their source transcript is deleted, while retaining credentials observed in other
  live transcripts
  ([#15](https://github.com/nameforjt-afk/session-knowledge/issues/15),
  [#20](https://github.com/nameforjt-afk/session-knowledge/pull/20)).
- Preserve private Claude configuration file permissions during uninstall and complete
  both standard and `--purge` removal without a nounset error
  ([#21](https://github.com/nameforjt-afk/session-knowledge/issues/21),
  [#22](https://github.com/nameforjt-afk/session-knowledge/pull/22)).
- Keep the MCP server version synchronized with the package version from a single source.

### Security

- Restrict transcript and code indexes, including SQLite WAL/SHM sidecars, to mode 0600.
  Existing database permissions are tightened automatically the next time each index opens.

## [0.1.1] - 2026-09-17

### Security

- Preserve existing Claude configuration file permissions during installation and create
  new configuration files as mode 0600.

## [0.1.0] - 2026-09-17

### Added

- Local indexing of Claude Code transcripts with FTS5-backed English and CJK search.
- Thirteen MCP tools for session retrieval, timelines, tool-call lookup, code discovery,
  and credential metadata.
- Separate permission-restricted credential vault and redacted full-text index.
- Incremental transcript indexing, code capability tags, duplicate detection, and
  installation/uninstallation scripts.
- A 31-test standard-library regression suite running on Linux and macOS with Python
  3.10 and 3.14 ([#1](https://github.com/nameforjt-afk/session-knowledge/issues/1),
  [#2](https://github.com/nameforjt-afk/session-knowledge/pull/2)).
- Contributor guidance, structured issue forms, pull request checks, and private security
  reporting ([#11](https://github.com/nameforjt-afk/session-knowledge/issues/11),
  [#12](https://github.com/nameforjt-afk/session-knowledge/pull/12)).

### Fixed

- Prevented placeholder-like substrings from bypassing secret redaction
  ([#3](https://github.com/nameforjt-afk/session-knowledge/issues/3),
  [#4](https://github.com/nameforjt-afk/session-knowledge/pull/4)).
- Enforced adjacency and ordering for multi-bigram CJK phrase queries
  ([#5](https://github.com/nameforjt-afk/session-knowledge/issues/5),
  [#6](https://github.com/nameforjt-afk/session-knowledge/pull/6)).
- Bounded every numeric MCP argument to prevent unbounded SQLite results
  ([#7](https://github.com/nameforjt-afk/session-knowledge/issues/7),
  [#8](https://github.com/nameforjt-afk/session-knowledge/pull/8)).
- Rejected ambiguous short session IDs instead of returning arbitrary session content
  ([#9](https://github.com/nameforjt-afk/session-knowledge/issues/9),
  [#10](https://github.com/nameforjt-afk/session-knowledge/pull/10)).

### Security

- Searchable text now redacts every value recognized as a secret even when the value looks
  like a placeholder. Placeholder classification remains available only for vault ranking.
- MCP result and pagination sizes are validated before database access.

### Known security boundary

`vault.db` intentionally contains plaintext credential values. It is restricted to the
current operating-system user but must never be committed, synced, or shared. Redaction is
pattern-based; custom secret formats may still require manual review with
`verify-redaction`.

[0.1.0]: https://github.com/nameforjt-afk/session-knowledge/releases/tag/v0.1.0
[0.1.1]: https://github.com/nameforjt-afk/session-knowledge/releases/tag/v0.1.1
[0.1.2]: https://github.com/nameforjt-afk/session-knowledge/releases/tag/v0.1.2
