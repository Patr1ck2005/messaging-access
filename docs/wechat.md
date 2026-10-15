# 微信读取

上游为 [wechatauto-replica 1.2.6.1](https://github.com/fanyuantaier/wechatauto-replica/tree/v1.2.6.1)。本项目只实例化 `WeChatDB` 和本地媒体接口，不调用 GUI 驱动、发送、客户端补丁或上游 GUI doctor。

当前已实测 Windows x64 / Python 3.11 / 微信 4.1.15.13 的数据库读取、历史分页、本人消息、跨分片定位、本地图片和用户新发消息。具体账号、计数和私人验收记录不随公开仓库分发。

安装后，AI 按 [部署配置定位](deployment.md) 选择唯一配置并解析 Python 与私有目录；新安装默认使用普通用户根，旧 AppData 配置兼容。权限读取流程见 [skill 微信引用](../skills/messaging-access/references/wechat.md)。微信由操作者正常登录；多个账号时，读取的稳定账号必须与已配置权限对应。

普通消息清单使用 `list_message_chats()`，CLI sessions 只含可见会话。最新查询重新实例化数据库视图以检查 WAL；历史按任务分页，完整导出另核对源清单及行数。定位消息保留 `username + shard + local_id`；不能跨分片只按 local_id 找媒体或发送者。

导入前设置私有 TEMP/TMP、工作目录和密钥目录，捕获标准输出／错误；导入后关闭默认文件日志。上游账号自愈可能更改传入的 workdir，须检查最终路径仍在 privateRoot 内。异常仅报告阶段和类型，原始日志留在本机。

普通查询复用解密缓存，源 DB 或 WAL 改变时会重建对应分片，刷新时有临时磁盘峰值。媒体、快照和异常临时文件的保留及清理见 [存储说明](storage.md)。不要删除客户端原数据库或密钥来腾空间。

本地未同步、已删、特殊业务记录和过期媒体可能缺失。图片按已在本机的 original/mid/thumb 档位读取；语音文件存在与语音识别是不同功能。需要界面操作才能取得的资源由用户手动处理。
