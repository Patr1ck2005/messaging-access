# 微信：wechatauto-replica 1.2.6.1

上游：[项目](https://github.com/fanyuantaier/wechatauto-replica)、[固定版本数据库源码](https://github.com/fanyuantaier/wechatauto-replica/blob/v1.2.6.1/wechatauto/db.py)、[媒体源码](https://github.com/fanyuantaier/wechatauto-replica/blob/v1.2.6.1/wechatauto/media.py)。部署版本与状态以配置 `projectRoot` 下的 README 为准。

## 初始化与会话发现

在 PowerShell 中把一次性 Python 代码通过标准输入交给项目环境。以下可作为一个进程直接执行，只输出去身份的计数与状态。先隔离临时目录、cwd 与密钥目录，再 import；上游导入会加载 GUI 模块，但这里不实例化或操作任何 GUI 类。

```powershell
$deploymentFile = if ($env:MESSAGING_ACCESS_CONFIG) {
    $env:MESSAGING_ACCESS_CONFIG
} else {
    Join-Path $env:LOCALAPPDATA 'MessagingAccess/deployment.json'
}
$deployment = Get-Content -LiteralPath $deploymentFile -Raw -Encoding utf8 | ConvertFrom-Json
if ($deployment.schemaVersion -ne 1) { throw 'Unsupported messaging-access deployment schema.' }
@'
import contextlib, io, json, os, tempfile
from pathlib import Path

config_path = Path(os.environ.get('MESSAGING_ACCESS_CONFIG') or
                   str(Path(os.environ['LOCALAPPDATA']) / 'MessagingAccess' / 'deployment.json'))
deployment = json.loads(config_path.read_text(encoding='utf-8-sig'))
if deployment.get('schemaVersion') != 1:
    raise SystemExit('Unsupported messaging-access deployment schema.')
private = Path(deployment['privateRoot']).resolve()
work, temp, keys = private / 'wechat', private / 'tmp', private / 'wechat-keys'
for directory in (work, temp, keys):
    directory.mkdir(parents=True, exist_ok=True)
os.environ.update(TEMP=str(temp), TMP=str(temp), WECHATAUTO_KEYS_DIR=str(keys))
tempfile.tempdir = None
os.chdir(work)
captured = io.StringIO()
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    try:
        from wechatauto.param import WxParam
        WxParam.ENABLE_FILE_LOGGER = False
        from wechatauto.db import WeChatDB, list_accounts
        accounts = list_accounts()
        db = WeChatDB(workdir=str(work))
        private_paths = all(Path(p).resolve().is_relative_to(private)
                            for p in (db.workdir, db.keys_file))
        if not private_paths:
            result = {'ok': False, 'error_code': 'PRIVATE_PATH_OUTSIDE_ROOT'}
        else:
            chats = db.list_message_chats()
            result = {'ok': True, 'chat_count': len(chats),
                      'message_count': sum(c['message_count'] for c in chats),
                      'local_account_directories': len(accounts),
                      'account_scope': 'library-selected readable local account',
                      'current_login_independently_confirmed': False,
                      'actual_work_and_keys_inside_private_root': True}
    except Exception as exc:
        result = {'ok': False, 'error_type': type(exc).__name__}
result['captured_warning_lines'] = sum('[WARNING]' in line for line in captured.getvalue().splitlines())
result['captured_error_lines'] = sum('[ERROR]' in line for line in captured.getvalue().splitlines())
print(json.dumps(result))
'@ | & $deployment.pythonExe -
```

`list_message_chats()` 返回 `md5, username, name, message_count`，汇总普通 `message_*.db` 的全部 `Msg_*` 表，包含隐藏历史。实际检索时在请求进程中用 `name/username` 找目标，仅输出任务需要的候选。CLI 可见会话列表不能证明全量覆盖。

上游先按数据库修改时间选择账号，还可能通过 `_try_other_accounts`／`_select_account_by_keys` 校验密钥后切换账号并改写 `workdir/keys_file`。因此必须同时设置私有 `TEMP/TMP` 并清除 Python 的 `tempfile.tempdir` 缓存；实际回退到 `private/tmp/wechatauto_db/...` 是允许的，不应误报为路径逃逸。初始化后验证实际路径仍位于私有根，不能只相信传入的 `workdir`。cwd 也移到私有工作目录，避免默认日志／下载路径落进项目。

`WxParam.ENABLE_FILE_LOGGER` 默认开启，import 后立即关闭。所有上游导入与数据库操作均留在 stdout/stderr 捕获范围；原始诊断可能包含账号、路径或密钥，不得裸打印或复制到 evidence。可以在进程内部分析警告，只输出计数、类型及去身份状态。`captured_warning_lines` 只计被格式化为 WARNING 的行，零不能单独证明无全部异常。

`list_accounts()` 只枚举本地目录；mtime 与密钥校验也不等于已独立确认当前登录身份。多账号时明确说明本次是库自动选择的可读本地账号；目标身份影响任务时先让用户确认目标，再传 `account=...` 并检查库最终选择是否相符，不默默分析另一账号。

## 最新与分页历史

```python
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    try:
        rows = db.get_messages(user='filehelper', limit=50, offset=0)
        message_result = {'ok': True, 'rows': rows}
    except Exception as exc:
        message_result = {'ok': False, 'error_type': type(exc).__name__}
# 正常消息查询可输出用户请求的行；部署验收把 rows 改为 len(rows)。
print(json.dumps(message_result, ensure_ascii=False, default=str))
```

后续代码都沿用上述成功初始化的同一进程上下文；失败时不继续。把 `filehelper` 换成已解析的 `username`。`get_messages(user, limit, offset)` 跨分片按 `sort_seq` 倒序合并，`offset` 按已读行数递增。保留返回的 `shard`；发送者也按本分片解析，不能跨分片沿用数字 `sender_id` 映射。

最新查询每次重新实例化 `WeChatDB`，以发现新建分片并检查数据库及 WAL 修改状态。WAL 合并失败、分片读取失败等警告意味着该次可能缺消息，应报告而非仅看列表是否非空。

连续有新消息时 offset 会移动，不用滚动 offset 声称严格完整快照。大范围历史采用上游导出，记录导出起止时间；活动账号导出期间仍可能写入，前后计数不同则报告差异，选择小范围重读或明确需要静止窗口。

## 全账号检索与计数验收

只有任务确需大范围历史检索时才使用导出。先检查既有快照是否覆盖任务范围；下面示例复用 `history.sqlite`，文件已存在时仅核对其内部计数，不再次导出或把它称为当前最新。确需刷新已有快照时先用项目缓存工具预览，按用户明确的刷新／清理要求处理后重建，不为每次查询创建一份时间戳全量副本。沿用上面已初始化的 `db/work`：

```python
import sqlite3

with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    try:
        snapshots = work / 'snapshots'
        snapshots.mkdir(exist_ok=True)
        target = snapshots / 'history.sqlite'
        reused = target.exists()
        if not reused:
            before = db.list_message_chats()
            receipt = db.export_history(str(target), fmt='sqlite', users=None, limit_per_chat=None)
        with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as conn:
            chat_rows = conn.execute('SELECT md5, message_count FROM chats').fetchall()
            actual = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            per_chat = conn.execute('SELECT c.md5, c.message_count, COUNT(m.rowid) '
                'FROM chats c LEFT JOIN messages m ON m.username=c.username '
                'GROUP BY c.md5, c.message_count').fetchall()
        export_result = {
            'ok': True, 'snapshot_reused': reused, 'sqlite_messages': actual,
            'per_chat_rows_match': all(declared == found for _, declared, found in per_chat),
        }
        if not reused:
            after = db.list_message_chats()
            expected = {c['md5']: c['message_count'] for c in before if c['message_count'] > 0}
            export_result.update(
                receipt_messages=receipt['messages'],
                receipt_matches_rows=receipt['messages'] == actual,
                source_counts_match=expected == dict(chat_rows),
                source_stable_during_export=before == after)
    except Exception as exc:
        export_result = {'ok': False, 'error_type': type(exc).__name__}
print(json.dumps(export_result))
```

表为 `chats(md5, username, name, message_count)` 和 `messages(username, local_id, type, type_code, sender_id, sender_name, create_time, content, server_id, md5, sort_seq)`。用参数化 SQL 查询，不拼接私人搜索词。`create_time` 为 Unix 秒。上游 SQLite schema 不保留 `shard`，媒体定位须重新从 `get_messages()`／原始行取准确分片。

回执计数等于写入行数只证明导出自洽。上游对部分分片错误可能跳过；必须同时核对源会话清单、每会话数量与警告。记录本次实际范围，不能把导出成功等同于全部历史已完整读取。

## 已在本机的媒体

仅在任务需要附件时保存媒体，先复用既有可用副本。沿用成功初始化上下文，在捕获范围内导入与调用 `MediaDownloader`。对选定消息使用具体方法并传回该行 `shard`：

```python
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    try:
        from wechatauto.media import MediaDownloader
        media = MediaDownloader(db, save_dir=str(work / 'media'))
        path = media.download_image(user, row['local_id'], shard=row['shard'], tier='best')
        media_result = {'ok': True, 'available': path is not None}
    except Exception as exc:
        media_result = {'ok': False, 'error_type': type(exc).__name__}
print(json.dumps(media_result))
# 其他类型：download_voice / download_video / download_file，均支持 shard=...
```

图片 `tier='original'` 只取本机已有原图，`best` 可能是压缩图或缩略图；结合 `image_status` 判断档位。语音可先看 `voice_status`。不使用 `download_image_original` 等操作微信界面的路径，也不调用不接受选定 shard 的通用 `download_media` 精确定位消息。

本机缺失返回 `None` 时报告不可取得。手机未同步的消息、已删记录、未落盘语音、过期媒体、特殊业务数据库不在普通消息库完整性声明内。

## 故障判断

- 找不到目录／账号：未登录、无本机数据库、目录迁移或自动发现不适用；按 README 既有配置指定 `db_dir`，不要造目录假装存在。登录和必要界面操作由用户手动完成，不使用 CU／GUI 自动化。
- 密钥或解密失败：检查微信是否登录和版本，只报去身份错误；不输出 `keys.json`、`master_key`、内存或配置全文。
- 最新消息未见：先确认已同步到桌面数据库，再查 WAL 警告、重建 `WeChatDB`；历史缓存可用不能代替最新验收。
- 包缺失／版本变化：使用项目固定环境与 README；不在查询任务中升级微信、替换上游或初始化 GUI。
