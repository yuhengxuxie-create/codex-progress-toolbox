# Security Policy

## Supported version

Security fixes are applied to the latest published release.

## Reporting a vulnerability

Do not open a public issue containing a webhook, signing secret, API key, thread
ID, conversation content, user path, log file, or exploit detail.

Prefer GitHub Private Vulnerability Reporting when it is enabled for the
repository. If it is unavailable, open a minimal public issue asking the
maintainer for a private reporting channel, without including sensitive details.

Include the affected version, impact, reproduction preconditions, and a redacted
proof of concept. Rotate any credential that may have been exposed before
continuing diagnosis.

## Scope notes

- Feishu webhook URLs are credentials.
- `config.local.json`, environment variables, `.state/`, backups, and Codex
  conversation data are local private material and must never be attached to an issue.
- The project intentionally guarantees normal completed-turn notifications only;
  missing interrupted or failed turns is a documented lifecycle boundary, not by
  itself a security vulnerability.
