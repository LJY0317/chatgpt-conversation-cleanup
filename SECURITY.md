# Security and privacy

ChatGPT Conversation Cleanup works with local ChatGPT/Codex metadata, so bug reports can accidentally expose private conversation or account information.

## Reporting a security issue

Please use GitHub's private security-reporting feature for vulnerabilities when it is available for this repository. Do not post credentials, authentication material, private conversation content, local databases, recovery files, or raw application logs in a public issue.

## Safe diagnostics

When sharing diagnostics publicly:

- remove names, email addresses, account/workspace identifiers, conversation IDs, local paths, and conversation titles that are not necessary to reproduce the issue;
- never attach `auth.json`, browser/session data, SQLite databases, recovery archives, or key/certificate files;
- prefer the utility's privacy-safe diagnostics and synthetic reproductions over raw ChatGPT Desktop data.

The repository `.gitignore` intentionally excludes common local database, recovery, credential, environment, log, and maintainer-state files to reduce accidental publication risk.
