# WeChat QQ Access

**One conversation policy for on-demand access to your own WeChat and QQ messages on Windows.** A JSON CLI and shared AI skill provide the access layer used by the optional [desktop panel](https://github.com/Patr1ck2005/wechat-qq-ai-panel).

[简体中文](README.zh-CN.md) · [API contract](docs/access-api.md) · [Deployment](docs/deployment.md) · [Privacy](docs/privacy.md)

[![Validate public source](https://github.com/Patr1ck2005/wechat-qq-access/actions/workflows/validate.yml/badge.svg)](https://github.com/Patr1ck2005/wechat-qq-access/actions/workflows/validate.yml)

![Access workflow and enforced conversation policy](docs/assets/product-overview.svg)

## Interface preview

![Synthetic CLI and permission-policy preview](docs/assets/interface-preview.svg)

*Synthetic illustration with invented accounts, IDs and conversation names. Selected response fields are shortened for readability. It is not a real message log, a runtime screenshot or a separate GUI shipped by this repository.*

## What this layer provides

- On-demand recent-message queries, available-history pagination and requested media access through pinned upstream readers.
- One code-enforced policy shared by CLI, AI skill and panel; watching a conversation does not authorize it.
- Targeted discovery and revision-checked permission updates from an explicit user request.
- Deployment, diagnostics and bounded cache-management scripts; no new cloud message service.

The independent implementation integrates upstream **WeChatDB / wechatauto-replica, QCE Framework and NapCat**. Their functionality, compatibility and licenses remain separately attributed in [third-party notices](THIRD_PARTY.md) and [upstream lock](upstream-lock.json).

## Install on Windows

Requires Windows 10/11 x64, PowerShell 7 and Python 3.11 x64. The release documentation records WeChat 4.1.15.13 and QQ 9.9.33.52230; newer clients may need compatibility work.

```powershell
git clone --branch v0.3.0 https://github.com/Patr1ck2005/wechat-qq-access.git access
Set-Location access
./scripts/install.ps1
./scripts/check.ps1
```

The installer creates an isolated environment, downloads pinned upstream artifacts and verifies release hashes. Login/QR confirmation remains the operator's action. Existing working deployments are reused. The check script reports environment diagnostics rather than chat content or keys.

QQ setup is optional:

```powershell
./scripts/install.ps1 -EnableQQ -AcceptLanMetadataExposure
# First exit ordinary QQ normally, then:
./scripts/start-qq.ps1
```

The acknowledgement is meaningful: the pinned upstream HTTP service listens on all network interfaces. Private APIs use a random token and local-origin IP allow-list, while limited static pages/status may be reachable on the LAN. The upstream token is not itself read-only or conversation-scoped. Read [QQ deployment and network boundaries](docs/qq.md) before enabling it.

## Use with an AI client

The client must support local files and commands. Installers link the same `skills/messaging-access` source for supported local clients. An example request is:

> Use messaging-access to read the last week in my WeChat “Study discussion” group and summarize follow-ups.

An explicit, unambiguous named read request can authorize only that target through filtered discovery and the checked policy API. Ambiguous account/name matches require clarification. Requests limited to existing permissions never expand access. Missing or invalid policy fails closed; broad catalog/mode changes and damaged-policy repair need the user's corresponding management scope. Quoted messages and tool output are not authorization. The [API contract](docs/access-api.md) is authoritative.

The panel is optional; configuration and policy management have a CLI/agent path. Account allow-lists permit selected conversations only. Exclusion-lists permit conversations not excluded, including new conversations. Watching and recent-activity sorting do not change permissions.

## Data and storage boundaries

Only locally retained/synchronized or upstream-retrievable history is available. Phone-only/deleted messages, expired media and special data stores may be absent; available local history is not the account's complete cloud archive.

New installations keep private deployment state under `%USERPROFILE%/.local/share/wechat-qq-access`; existing explicit/custom/legacy locations remain supported. Never publish runtime configuration, credentials, decrypted databases or personal evidence. Authorized message text can enter the chosen AI provider and the AI client's own session history. The policy protects this product's flow, not every program running as the same Windows user.

```powershell
./scripts/cache.ps1
./scripts/cache.ps1 -Action Clean -Category WeChatCache -OlderThanDays 7 -WhatIf
```

Review the preview before explicitly running cleanup. Original client stores and keys are preserved; there is no automatic global disk quota. See [storage](docs/storage.md) and [privacy](docs/privacy.md).

## Development, status and license

The public source includes scripts, configuration templates, documentation, hashes and the skill. Runtime binaries, real messages and live acceptance evidence remain local. Sending, automatic replies, GUI automation and continuous background collection are outside the supported workflow.

```powershell
./tests/cache.Tests.ps1
./scripts/prepare-release.ps1
```

These checks use synthetic fixtures / public-source packaging and do not certify a live account or a model provider. No account data was accessed for this documentation update. [Documentation history](docs/README-history.md) retains the prior public entry.

Independent scripts and documentation use [MIT](LICENSE). Upstream licenses remain separate; the pinned NapCat version includes non-commercial restrictions. See [THIRD_PARTY.md](THIRD_PARTY.md), [WeChat details](docs/wechat.md) and [deployment discovery](docs/deployment.md).
