# 迁移版本

`20261116_assistant` 为已有核心表的数据库添加助手会话与消息表，依赖 `users` 已存在。

`20261117_trace` 接在助手迁移之后，创建 `traces` 和 `spans`，并在已有的 `runner_attempts` 增加可空的 `trace_id`。已有库在启动新版本前执行 `python -m alembic upgrade head`。
全新开发库和已有自管迁移链的接入方式见 [评测助手](../../docs/assistant.md#启用)。
