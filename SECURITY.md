# Security policy

session-knowledge processes local transcripts and may store credential values in a
permission-restricted vault. Please treat potential leaks as sensitive.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities involving credential exposure, unsafe file
permissions, transcript disclosure, or arbitrary code execution. Use GitHub's private
vulnerability reporting for this repository instead:

https://github.com/nameforjt-afk/session-knowledge/security/advisories/new

Include a minimal reproduction built from synthetic values. Never attach a real
`~/.claude` directory, `vault.db`, `.env` file, transcript, API token, or password.

You should receive an initial acknowledgement within seven days. Details will remain
private until a fix is available and coordinated disclosure is appropriate.

## Supported versions

Security fixes are applied to the latest code on `main`. Until versioned releases are
published, users should update to the latest commit before reporting an already-fixed
problem.

## Security boundaries

- Redaction is pattern-based and cannot guarantee detection of every custom secret format.
- `vault.db` intentionally contains plaintext values and must never be synced or shared.
- Local processes running as the same operating-system user may be able to read the vault.
- Search results should contain fingerprints rather than plaintext secrets; run
  `verify-redaction` periodically to audit the index.
