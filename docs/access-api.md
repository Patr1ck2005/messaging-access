# Permission-enforced access API

`scripts/access.py` is a local JSON command interface, not a network service. Invoke it with the Python executable in the deployment configuration. Write exactly one UTF-8 JSON request to stdin; stdout contains one `{ok,data}` or `{ok:false,error:{code,message}}` object. Internal upstream output is captured, never echoed.

Configuration follows [deployment discovery](deployment.md). An optional command-line `--config <file>` precedes the environment and new/legacy default locations. Request JSON cannot override deployment or policy paths; an invalid selected config never falls back to another installation.

| Action | Input | Purpose |
|---|---|---|
| `catalog` | none | Trusted local setup metadata: available accounts and conversations; never send this unrestricted catalog to a model |
| `policy.get` | none | Current policy or `policy:null` when setup is required |
| `policy.set` | `accounts` | Human/local administration only: select mode and conversation IDs per account |
| `conversations` | none | Only conversations allowed by the current policy |
| `authorize` | `refs` | Check the entire selected set and return the current revision |
| `messages` | `refs`, `since`, `until` | Read every available page in the fixed Unix-seconds interval |
| `media` | `ref`, `message_id` | Check conversation permission before resolving the source message; no caller-supplied filesystem paths |

Each conversation has an opaque stable `ref` and separate `platform`, `account`, `conversation_id`, `title`, `kind`, `last_active` metadata. `last_active` is a finite nonnegative Unix-seconds number or `null` when unknown. It is the last-message time available in source metadata, not an activity score or the catalog refresh time: WeChat uses `SessionTable.last_timestamp`, and QCE v6.3.2 recent contacts use the zoned ISO `lastMsgTime` field. Epoch milliseconds are normalized to seconds. Contacts absent from recent/session metadata retain `null`; callers can put these last and preserve source order for ties. No message-body scan is added for sorting. Both `catalog` and permission-filtered `conversations` expose the same projection.

Authorize by platform/account/ID, never title or timestamp. Message IDs preserve source identity across shards. Pagination completion describes the local/source interface's coverage, not all phone or server history.

The policy is private runtime state, separate from the watched list. An unconfigured account, invalid policy or permission failure cannot fall back to unrestricted access. Changing permissions changes the revision; callers must authorize again before each model request and before displaying retained results. The companion panel does this automatically. Already-submitted provider data cannot be recalled by a local permission change.

## Standalone local setup

The panel is optional. A human can run `catalog` locally, choose the account and stable conversation IDs, then submit this shape to `policy.set`:

```json
{"action":"policy.set","accounts":[{"platform":"wechat","account":"ACCOUNT_FROM_LOCAL_CATALOG","mode":"allow","conversations":["CONVERSATION_ID_FROM_LOCAL_CATALOG"]}]}
```

Use `mode:"deny"` for an exclusion list. An empty allow-list permits none; an empty exclusion-list permits all conversations for that configured account, including future ones. Configure each account explicitly. Do not copy real catalog output into issues, public files or an AI context for permission setup.

Only local setup should call `catalog` or `policy.set`. The skill uses `conversations`, `authorize`, `messages` and `media`; it must not offer a raw-library or direct-QCE fallback. A user-controlled full-permission shell remains outside this product boundary.
