# Contributing

Thank you for improving Codex Progress Toolbox.

1. Create a focused branch.
2. Keep the runtime dependency-free unless a strong need is documented.
3. Add or update standard-library `unittest` coverage.
4. Run `python -m unittest discover -s tests -v`.
5. Run `scripts\package-share.ps1` on Windows before a release-oriented change.
6. Describe behavior changes and privacy implications in the pull request.

Never commit real configuration, webhooks, signing secrets, API keys, thread IDs,
conversation content, logs, backups, user paths, server addresses, or SSH material.
Use `example.invalid`, loopback addresses, documentation IP ranges, and obvious test
tokens in fixtures.

Keep public error messages and logs redacted. A remote response may echo request
content, so tests should verify that rejection paths do not expose response bodies.
