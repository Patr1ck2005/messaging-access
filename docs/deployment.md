# Deployment configuration discovery

All access entry points select exactly one configuration, in this order:

1. An explicit `-ConfigPath` in PowerShell, or `--config` in `scripts/access.py`.
2. `MESSAGING_ACCESS_CONFIG`, when set for the calling process.
3. `%USERPROFILE%/.local/share/wechat-qq-access/deployment.json`, when it exists.
4. The legacy `%LOCALAPPDATA%/MessagingAccess/deployment.json`, when it exists.

When neither default exists, the installer creates the new user-root location; read commands report that setup is unavailable. A missing explicit/environment path, malformed selected JSON, unsupported schema or invalid referenced installation stops that operation. None permits a fallback to another installation. A selected directory named `deployment.json` is also an error. No registry lookup or global environment-variable registration is required.

The new default private root is `%USERPROFILE%/.local/share/wechat-qq-access`. This ordinary user directory is outside AppData because an MSIX-hosted AI client may virtualize AppData files, leaving ordinary Explorer/desktop processes with a different view. Existing legacy installations remain supported; discovery does not copy, migrate or delete their data. If both defaults exist, the new default wins. Use an explicit path to select another installation deliberately.

PowerShell entry points share `scripts/deployment-path.ps1`; Python uses `wechat_qq_access.deployment`. The shared skill follows the same precedence before loading the selected `pythonExe` and `projectRoot`. Never infer a second deployment from the skill folder, working directory or an old virtual environment.

```powershell
# Explicit custom configuration remains supported.
./scripts/check.ps1 -ConfigPath <your-deployment.json>
./scripts/cache.ps1 -ConfigPath <your-deployment.json> -Json
```

The deployment stores machine paths, not model API keys. Existing `privateRoot`, QQ runtime, security-file and client paths are preserved when reusing a configuration. New installation supports explicit `-PrivateRoot` and `-ConfigPath`; choose a dedicated private directory. Cleanup recognizes dedicated roots named `wechat-qq-access` and the legacy `MessagingAccess`, preserves keys/original stores and never broadens its boundary merely because a different configuration was selected.
