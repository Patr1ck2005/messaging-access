# QQ：QCE v6.3.2 Framework HTTP API

上游：[固定发行版](https://github.com/shuakami/qq-chat-exporter/releases/tag/v6.3.2)、[消息路由](https://github.com/shuakami/qq-chat-exporter/blob/v6.3.2/qq-chat-export-server/src/api/routes/messages.rs)。部署目录、当前启动状态与启动／停止方式以配置 `projectRoot` 下的 README 为准。

本机调用地址是 `http://127.0.0.1:<qqPort>`，端口取配置 `qqPort`（默认 40653）。上游该版实际监听 `0.0.0.0`，不能因为使用本机 URL 就声称仅监听回环；当前部署的网络限制与是否启动以 README 为准。QCE 由 Framework 启动的 QQ 登录实例提供数据，日常 QQ 已登录不证明 QCE 接口已运行。

查询前检查现成 `GET /health`：须处于 `mode=plugin` 且 `online=true`，不能把 standalone 模式的健康 200 当成 QQ 消息可读。运行实例停止后，应按 README 使用配置 `qqRoot` 下官方 `napiLoader` 及配置 `qqExe` 重新启动；直接打开 QQ 不会注入本部署。需要登录时由用户手动操作。

## 认证与发现

认证文件由运行配置 `qqSecurityFile` 定位。在一次性请求进程内部读取其中 `accessToken`，使用 `Authorization: Bearer ...`。QCE 启动器的 `QCE_CONFIG_DIR` 只作用于其启动进程，新 Codex／DSH 请求进程加载相同的 deployment 配置，不依赖该变量，也不退回用户全局旧 `~/.qq-chat-exporter/security.json`。不要把 token 放 URL 或展开成命令参数，不输出安全文件、请求 headers 或含 token 的异常全文。实际服务验收状态见 README。

以下只用标准库直接调用既有接口，没有新增持久化 wrapper。普通诊断输出计数；正常消息任务按需输出所需内容。

```powershell
$deploymentFile = if ($env:MESSAGING_ACCESS_CONFIG) {
    $env:MESSAGING_ACCESS_CONFIG
} else {
    Join-Path $env:LOCALAPPDATA 'MessagingAccess/deployment.json'
}
$deployment = Get-Content -LiteralPath $deploymentFile -Raw -Encoding utf8 | ConvertFrom-Json
if ($deployment.schemaVersion -ne 1) { throw 'Unsupported messaging-access deployment schema.' }
@'
import json, os
from pathlib import Path
from urllib.request import Request, urlopen

config_path = Path(os.environ.get('MESSAGING_ACCESS_CONFIG') or
                   str(Path(os.environ['LOCALAPPDATA']) / 'MessagingAccess' / 'deployment.json'))
deployment = json.loads(config_path.read_text(encoding='utf-8-sig'))
if deployment.get('schemaVersion') != 1:
    raise SystemExit('Unsupported messaging-access deployment schema.')
base_url = 'http://127.0.0.1:' + str(int(deployment['qqPort']))
security = json.loads(Path(deployment['qqSecurityFile']).read_text(encoding='utf-8-sig'))
req = Request(base_url + '/api/friends?page=1&limit=999&forceRefresh=true',
    headers={'Authorization': 'Bearer ' + security['accessToken']})
with urlopen(req, timeout=30) as response:
    result = json.load(response)
if result.get('success') is not True:
    raise RuntimeError('QCE API failed: ' + str(result.get('error', {}).get('type', 'unknown')))
data = result['data']
print(json.dumps({'returned': len(data['friends']), 'has_next': data['hasNext']}))
'@ | & $deployment.pythonExe -
```

`GET /api/friends` 和 `GET /api/groups` 支持 `page, limit, forceRefresh`；数据在 `data.friends`、`data.groups`，分页字段为 `totalCount, currentPage, totalPages, hasNext, hasPrev`。`limit` 最大 2000。先读第一页刷新，再逐页读取到 `hasNext=false`，不能把第一页当全量。

好友接口的 `uid`（`u_*` 字符串）用于私聊 `peerUid`，群接口的 `groupCode` 用于群聊。不要把数字 QQ 号当好友 UID。临时会话等补充 `GET /api/recent-contacts?limit=2000&includeAll=true`，返回 `data.contacts, totalCount, rawCount`，无分页。`includeAll` 会查询更多同步／本机会话，但仍不能称为全部历史会话完整枚举。

设备或特殊会话须把 recent contacts 返回的 `peerUid`、`chatType` 原样传给消息接口；例如设备会话可能为 `chatType: 8`，不能强行改为好友私聊的 `chatType: 1`。普通好友 `1`、群聊 `2` 的示例继续适用。

## 最新消息

以下片段沿用上面成功加载 deployment、`base_url` 和 `security` 的同一请求进程；把已解析的好友 UID 赋给 `chosen_friend_uid` 后直接调用上游接口。

```python
body = {
    'peer': {'chatType': 1, 'peerUid': chosen_friend_uid},
    'page': 1, 'limit': 50, 'forceRefresh': True,
}
req = Request(base_url + '/api/messages/fetch',
    data=json.dumps(body).encode('utf-8'), method='POST',
    headers={'Content-Type': 'application/json',
             'Authorization': 'Bearer ' + security['accessToken']})
with urlopen(req, timeout=120) as response:
    result = json.load(response)
if result.get('success') is not True:
    raise RuntimeError('QCE message fetch failed: ' + str(result.get('error', {}).get('type', 'unknown')))
data = result['data']
print(json.dumps({'returned': len(data['messages']), 'cache_hit': data['cacheHit'],
                  'fetched_at': data['fetchedAt'], 'has_next': data['hasNext']}))
```

群聊用 `chatType: 2`、`peerUid: chosen_group_code`。响应消息在 `data.messages`，保留消息 ID、时间、发送者与媒体元素。原始消息 `msgTime` 是 Unix 秒，下面的筛选 `startTime/endTime` 使用毫秒，不能混用。接口默认缓存约 10 分钟，**最新查询第一页必须 `forceRefresh: true`**；检查 `cacheHit` 与 `fetchedAt`，异常时不能把缓存称为最新读取。

## 历史分页

先固定 `filter: {'startTime': start_ms, 'endTime': end_ms}`，两个值均为 Unix 毫秒。同一窗口第一页 `forceRefresh: true`，之后增大 `page` 并用 `forceRefresh: false`，保持 peer、filter、limit 不变，直到 `data.hasNext=false`。每页同时检查 HTTP 状态与 JSON `success`，失败不能当作自然结束。

结果倒序。`data.totalCount` 只表示本次已取得／缓存的数据量，不能证明云端和手机全部历史已取得；按需缩小时间段核对边界、去重及补页。数据多时复用上游批量能力，不为节省请求删掉群聊、本人发言或消息类型。

## 故障与媒体

- 连接拒绝或无监听：QCE 未启动／启动失败，按 README 状态报告；尚未启用的服务不自动启动，不改请求到其他账号或另起机器人登录。登录和必要界面操作由用户手动完成，不使用 CU／GUI 自动化。
- HTTP 401／403 或 `success=false`：认证或授权失败，在进程内重读认证文件；`TOKEN_EXPIRED` 按配置 `projectRoot` 下的 `docs/qq.md` 续期说明使用上游配置热加载（默认有效期 7 天）。不输出 token，不把 401 当闭环成功。
- QQ 未登录、缓存陈旧、当前版本不支持：报告具体缺口，不从普通 QQ 窗口可见推定 QCE 可读。
- 媒体元素只证明消息含附件。声称媒体可用前须核对现成接口的本地资源／实际可取得文件；失效 URL、过期或未下载附件保留状态。首版不自动点击 QQ 或发送消息补资源。
