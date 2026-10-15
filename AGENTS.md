# messaging-access

Windows deployment kit and shared AI skill for on-demand reading of the operator's own WeChat and QQ data.

- Reuse the pinned upstream WeChatDB library and QCE Framework. This repository is the permission-enforcing access layer for the companion desktop panel and shared skill. Add a small JSON command interface and one shared conversation policy; do not add a messaging server, collector, model gateway or MCP wrapper.
- Read access is the supported workflow. Do not send messages, automate replies, patch WeChat UI accessibility, or use computer/GUI automation during development and acceptance.
- Select private configuration using [deployment discovery](docs/deployment.md): explicit argument, environment override, existing user-root default, then existing legacy AppData default. New installs use `%USERPROFILE%/.local/share/wechat-qq-access`; never retry another installation after a selected config is invalid. Keep machine-specific paths out of public source.
- Never print credentials, keys, raw private logs or complete runtime configurations. Diagnostics contain versions, counts, times, booleans and exception types. Ordinary user-requested analysis may read the requested message content.
- Keep account data, decrypted databases, media, runtime binaries, personal evidence and local deployment notes outside the public release. `vendor/`, `.venv/`, `evidence/` and `.local/` are local only.
- Avoid repeated history snapshots and unsolicited media copies. Cleanup defaults to preview, removes only recognized rebuildable files with explicit user action, preserves original client stores and all keys, skips active readers and reparse paths, and records actual removals.
- Preserve existing installations and settings. Never overwrite another skill or rotate working credentials as a routine install step. QQ startup requires the operator to exit normal QQ; never force-kill it.
- Fixed upstream checksums and independent upstream licenses remain explicit. Do not repackage upstream binaries or represent upstream functionality as this project's original implementation.
- Do not run model tests in another AI client without the operator's instruction. Live account acceptance is separate from offline script tests; label test coverage accurately.
- Git operations must stay within this project. Before committing, follow the operator's provenance convention; no automatic external publication.
