# QQ 部署与维护

复用 [QCE v6.3.2 Framework](https://github.com/shuakami/qq-chat-exporter/releases/tag/v6.3.2)，包含 NapCat 4.18.28。使用已安装的桌面 QQ 与现有登录；不复制 QQ 数据目录或另建机器人账号。本项目已实测 QQ 9.9.33.52230 的好友／群分页、私聊／群历史、本人发言、本地图片及设备会话的新消息。

## 安装和启动

```powershell
.\scripts\install.ps1 -EnableQQ -AcceptLanMetadataExposure
# 保存草稿，从托盘正常完全退出 QQ，然后：
.\scripts\start-qq.ps1
.\scripts\check.ps1
```

启动脚本解析 deployment.json，调用上游 `napiLoader.bat`，共用桌面客户端。普通 QQ 快捷方式不会自动加载本部署。脚本不强杀 QQ，不自动登录；登录确认由操作者完成。没有设置开机自启，退出 QQ／注销／重启后需要重新通过此入口启动。

停止首选 QQ 托盘的“退出”。随后检查接口端口及桥接端口是否停止监听；如果官方 server 残留，先核对 ExecutablePath 确为本部署的 qce-server.exe，再由操作者停止准确 PID，不按名称批量终止其他项目。

## 网络边界

本固定版本 [HTTP 服务](https://github.com/shuakami/qq-chat-exporter/blob/v6.3.2/qq-chat-export-server/src/main.rs#L179) 监听 `0.0.0.0:40653`。serverHost 配置影响显示地址，不能把监听改为仅回环。

安装器为私有 API 配置随机 token，加真实 TCP 来源 IP 白名单 `127.0.0.1/::1`，不相信 X-Forwarded-For。静态页面、根服务说明和有限 health/security-status 仍可能被局域网访问；QQ 桥接仅本机，NapCat 管理页面关闭。不配置端口转发。

因此启用 QQ 需要操作者显式传 `-AcceptLanMetadataExposure`。如部署环境不能接受该边界，请先只使用微信接入，或在明确理解网络边界后自行配置系统防火墙。本项目不静默修改防火墙。

读取说明只调用查询路由，底层 QCE token 没有独立的只读权限；不要把 token 交给外部服务。认证文件位置由 deployment.json 的 qqSecurityFile 指定，token 仅在请求进程内读取。

## 请求和认证

查询前 health 须为 `mode=plugin, online=true`；standalone 的 HTTP 200 不代表账号消息可读。最新消息第一页必须 `forceRefresh=true`，历史分页固定毫秒时间窗；原始 msgTime 是秒。详情见 [QQ 直接 API 示例](../skills/messaging-access/references/qq.md)。设备会话沿用 recent contacts 返回的原生 chatType，不能强制当作好友私聊。

上游默认 token 有效期七天，请求时重新读配置。到期后可正常退出并重新用启动器启动，上游加载过期配置时会更新。也可在保留白名单和其他字段的前提下，将 tokenExpired 设置为过去时间并原子替换本机配置，运行中的上游热加载会轮转 token；此路径已实测。不得打印旧／新 token，也不要把凭据留在备份或公开 evidence。

## 数据和日志

普通 fetch 的 [消息缓存](https://github.com/shuakami/qq-chat-exporter/blob/v6.3.2/qq-chat-export-server/src/api/routes/messages.rs#L599-L847) 在内存，最多 64 项、每项最多 20,000 条，逻辑有效期十分钟。它不自动创建完整历史导出或下载所有附件。

运行资料以 JSONL 等形式保存在 `%USERPROFILE%/.qq-chat-exporter`；QCE_CONFIG_DIR 只隔离认证配置。媒体下载和导出均按需；不要把旧版 Documents 导出目录当作本次可清理缓存。

NapCat 日志有轮转，但 QCE 部分日志会持续追加，且不能推定媒体会自动在三十天后删除。日志可能含私人信息和上游登录 URL，不回显到模型；清理入口及保留规则见 [存储说明](storage.md)。
