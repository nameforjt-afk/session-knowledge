# Contributing

Thanks for helping improve session-knowledge. Contributions that make retrieval safer,
more predictable, or easier to install are especially welcome.

## Before opening an issue

- Search existing issues first.
- Reduce bugs to the smallest synthetic transcript or code sample that reproduces them.
- Never paste a real Claude Code transcript, credential, `.env` file, or session index.
- Use GitHub's private vulnerability reporting for anything that could expose secrets.

## Local setup

The runtime has no third-party dependencies. Clone the repository and run the standard
library test suite:

```bash
git clone https://github.com/nameforjt-afk/session-knowledge.git
cd session-knowledge
python3 -m unittest discover -s tests -v
python3 -m compileall -q sessionmcp
```

Python 3.10 or newer with SQLite FTS5 support is required.

## Change workflow

1. Open or choose an issue that describes observable behavior.
2. Create a focused branch such as `fix/123-short-description`.
3. Add one failing regression test using synthetic data.
4. Make the smallest change that passes the test.
5. Run the complete suite and compile check.
6. Open a pull request with `Closes #123`, the reproduction, and verification output.

Tests should use public module interfaces and real temporary SQLite databases where
practical. Avoid mocks of internal modules and avoid assertions tied to implementation
details.

## Pull request expectations

- Keep one behavioral change per pull request.
- Explain user-visible behavior and compatibility impact.
- Include tests for fixes and new behavior.
- Keep runtime dependencies at zero unless an issue has established a strong reason to
  change that constraint.
- Update English and Chinese documentation together when user-facing behavior changes.
- Do not commit generated indexes, logs, local Python paths, or credentials.

All CI checks must pass before merge.
