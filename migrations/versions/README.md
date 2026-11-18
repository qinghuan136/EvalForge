# 迁移版本

`20261116_assistant` 为已有核心表的数据库添加助手会话与消息表，依赖 `users` 已存在。

`20261117_trace` 接在助手迁移之后，创建 `traces` 和 `spans`，并在已有的 `runner_attempts` 增加可空的 `trace_id`。已有库在启动新版本前执行 `python -m alembic upgrade head`。
全新开发库和已有自管迁移链的接入方式见 [评测助手](../../docs/assistant.md#启用)。

`20261118_conversation_store` 接在 Trace 迁移之后，新增 `assistant_conversation_records` 和 `assistant_conversation_entries`，包含归属、共享历史保护和执行租约约束；不修改旧助手表、不迁移历史数据。执行 `python -m alembic upgrade head` 可建表，但助手仍使用现有 fake 链路，数据库 Backend 后续接入。结构见 [ConversationStore 方案](../../docs/ConversationStore方案.md)。
