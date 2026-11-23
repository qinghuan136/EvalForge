# 迁移版本

`20261116_assistant` 为已有核心表的数据库添加助手会话与消息表，依赖 `users` 已存在。

`20261117_trace` 接在助手迁移之后，创建 `traces` 和 `spans`，并在已有的 `runner_attempts` 增加可空的 `trace_id`。已有库在启动新版本前执行 `python -m alembic upgrade head`。
全新开发库和已有自管迁移链的接入方式见 [评测助手](../../docs/assistant.md#启用)。

`20261118_conversation_store` 接在 Trace 迁移之后，新增 `assistant_conversation_records` 和 `assistant_conversation_entries`，包含归属、共享历史保护和执行租约约束；不修改旧助手表、不迁移历史数据。执行 `python -m alembic upgrade head` 可建表，但助手仍使用现有 fake 链路，数据库 Backend 后续接入。结构见 [ConversationStore 方案](../../docs/ConversationStore方案.md)。

`20261119_experiment_config` 创建版本化配置表和 Run 外键，移除旧配置表。已有开发库可执行 `python -m alembic upgrade head`。迁移要求实验和旧配置表为空，检测到旧记录会中止。用当前 ORM 全量建表的新库沿用 `alembic stamp head`。回退使用升级前的备份。

`20261123_code_evaluators` 将 Python 评分器合并到 `evaluators`，新增草稿列。旧源码版本按各评分器的行 ID 顺序编号，保留版本行 ID、源码、发布时间。旧 CODE 记录须已有所有者，否则升级中止。旧子表的语言、映射和沙箱配置随子表移除，回退使用升级前的备份。

`20261123_code_evaluator_archive` 新增可空的 `evaluators.archived_at`，已有评分器默认为未归档。
