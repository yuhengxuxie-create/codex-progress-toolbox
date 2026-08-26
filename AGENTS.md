# Codex project instructions

## Purpose

This repository is a public template for installing Codex progress notifications
on Windows. When the user asks to install, set up, configure, repair, or verify
the toolbox, follow the workflow below and keep a visible checklist.

## Safety rules

- Never ask the user to paste a Feishu webhook, signing secret, API key, real
  thread ID, or other credential into chat.
- Let the user enter secrets only in a local no-echo terminal prompt provided by
  `scripts\configure-feishu.ps1` or another repository script designed for it.
- Never print, read back, log, commit, or include secret values in a screenshot.
- Do not read or display `config.local.json`, its backups, `.state/`, Codex
  conversations, or user environment-variable values unless a specific
  diagnostic requires it. Prefer schema/presence checks that do not reveal values.
- Do not send a real external test message until the user explicitly agrees.
  `scripts\send-test.ps1 -DryRun` is safe to run without external delivery.
- Do not replace or delete the user's whole Codex config. Use the included
  installer and uninstaller, which preserve existing `notify` configuration.
- Keep this project in a stable path after installation because Codex stores the
  absolute notification entry path.

## First installation workflow

1. Read `README.md`, `INSTALL_WITH_CODEX.md`, and `docs/FEISHU.md`.
2. Confirm the host is Windows and that the user opened the extracted repository
   root. Check required commands without changing external state.
3. Guide the user through creating a Feishu group custom bot:
   - explain that this project needs a group custom bot, not an enterprise app;
   - recommend a private or trusted group;
   - recommend signature verification;
   - if keyword verification is enabled, recommend `对话名称`;
   - remind the user not to paste the webhook or signing secret into chat.
4. Wait until the user confirms that the webhook and optional signing secret are
   ready. Do not infer or fabricate them.
5. Run `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1` from the
   repository root. Report the non-secret result.
6. Open an interactive local terminal and run
   `.\scripts\configure-feishu.ps1`. Ask the user to type the webhook and signing
   secret into its no-echo prompts. If interactive input is unavailable, show the
   exact command for the user to run locally and wait for confirmation.
7. Help the user select conversations:
   - prefer `scripts\manage-threads.cmd` for the GUI; or
   - run `scripts\list-threads.ps1` and let the user edit only their local config.
   Do not paste real thread IDs into documentation, commits, or public output.
8. Explain the optional classifier. It is valid to leave it disabled or without
   an API key; Codex login is not the same as an OpenAI API key.
9. Run:
   - `.\scripts\validate.ps1`
   - `.\scripts\send-test.ps1 -DryRun`
   - `python -m unittest discover -s tests -v` when a compatible Python is present.
10. Ask for explicit permission before running `.\scripts\send-test.ps1` without
    `-DryRun`. Confirm the Feishu message arrived.
11. Tell the user to completely quit and restart Codex. Then help them run one
    monitored test turn and verify the notification.

## Troubleshooting order

1. Determine whether the completed turn was normal rather than interrupted or failed.
2. Run `scripts\validate.ps1` and inspect only redacted diagnostics.
3. Confirm that the selected value is an exact full thread ID without displaying
   it unnecessarily.
4. Check that the Feishu bot security settings match the local configuration:
   signing secret, keyword, and IP allowlist.
5. Confirm Codex was fully restarted after configuration changes.
6. If a secret may have leaked, stop troubleshooting and instruct the user to
   rotate the Feishu webhook/signing secret or API key first.

## Development checks

For code changes, run `python -m unittest discover -s tests -v`. Before packaging,
run `scripts\package-share.ps1`; it must reject local configs, backups, state,
logs, bytecode, archives, real thread IDs, credentials, and machine-specific paths.
