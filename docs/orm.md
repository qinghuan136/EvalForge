# 核心 ORM

基于 Phoenix commit `14c193cfa`，按云端 + 轻客户端方案调整；当前 21 张表、183 个字段，含停用后保留的旧配置表，以及身份与凭证表。
这里定义存储结构；尚未连接 PostgreSQL、执行建表或把 V0 Runner 接入数据库。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| `evalforge/db/base.py` | ORM 基类、JSONB / 时区时间类型、约束命名、暂缓外键标记 |
| `evalforge/db/orm/datasets.py` | Dataset、Version、Example、Revision |
| `evalforge/db/orm/experiments.py` | Experiment、样本快照关联、TaskRun、EvalRun |
| `evalforge/db/orm/attempts.py` | 云端 Attempt、完整派发/结果快照、活动容量约束 |
| `evalforge/db/orm/auth.py` | User、Runner、RunnerCredential；字段和 ER 图见 [鉴权数据结构](auth-schema.md) |
| `evalforge/db/orm/evaluators.py` | 评分器公共定义、三个子类型、源码版本、数据集使用配置 |
| `evalforge/db/orm/jobs.py` | 旧后台配置已停用；保留 Prompt、EVAL_ONLY 和原评分关联的表与外键 |
| `evalforge/db/models.py` | 统一导入和注册入口，Alembic 由此获得完整 metadata |

```python
from evalforge.db.models import Dataset, DatasetVersion, ExperimentRun

dataset = Dataset(name="价格测试集", metadata_={"purpose": "regression"})
version = DatasetVersion(dataset=dataset, description="初始版本")
# 由 AsyncSession 的 add / flush / commit 完成保存；创建 Python 对象本身不写数据库。
```

现有 `evalforge/models.py` 继续保留 dataclass，不改动 V0 的 Task / Evaluator 接口。
`TaskRun` 在 ORM 中对应 `ExperimentRun`，`EvalRun` 对应 `ExperimentRunAnnotation`。
Result 不建独立表。Runner 身份使用独立的 runners 表，与本地 V0 的执行器概念分开。

## 云端远程执行模型

```text
Experiment（整体状态、execution_config）
  → ExperimentRun（逻辑 Job、排队到最终结果）
    → RunnerAttempt（一次派发尝试，可保留多次历史）
```

- Experiment.status：pending / running / cancelling / completed / failed / cancelled；默认 pending。execution_config 默认为 {}，可保存 runner_id、agent_target_id、timeout_seconds；不保存客户端 URL/请求头。
- ExperimentRun.job_id 为对外唯一标识，attempt_id 为尝试标识；默认 ORM 插入时生成 UUID 字符串。内部主键保持整数。构造对象本身尚未生成这些默认值，发消息前须先 flush，或显式提供 ID。
- Run 默认 queued；assigned / running / unknown 均未产生最终结果。终态为 succeeded / failed / cancelled；output 包装成 `{"task_output": ...}`，有效 JSON null 与未产生结果的 SQL NULL 区分。
- Run.error 改为 JSON `{code, message}`；HTTP 状态、客户端 duration_ms 单独保存。实际起止时间可空，不能用云端收到通知的时间伪造；latency_ms 优先 duration_ms，其次实际时间差，否则为空。
- Attempt 保存 dispatch_json / result_json 与归属、取消意图、云端接收时间。runner_id 已建立到 runners.runner_id 的 RESTRICT 外键及普通索引；身份和凭证记录已定义，实际鉴权后续实现。
- Attempt 的 assigned / accepted / running / unknown 占活动容量；部分唯一索引分别约束每个 Runner、每个 Job 最多一个活动 Attempt。明确拒绝用 rejected + rejection_reason；超时不等于拒绝。
- 旧 ExperimentJob 的 table.info 标记 deprecated；Experiment.job 仅保留兼容关系。新实验不要求建立旧配置记录，也不新增远程配置子表。

本阶段只定义 ORM 与约束。消息快照的字段形状、标识一致性、配置冻结、结果幂等与不可重写、采用哪次 Attempt、实验状态推进由后续写入服务实现；不自动重试或发送 ACK。评分表结构保持原样。

## 数据类型与继承

- PostgreSQL JSON 字段使用 JSONB，时间字段使用 TIMESTAMP WITH TIME ZONE；写入时间应带时区。
- `metadata_` 是 Python 属性名，对应数据库列 `metadata`。
- Evaluator 使用 joined-table inheritance，通过 `(kind, id)` 复合外键关联子表。
- ExperimentPromptTask 通过 `(type, id)` 关联 ExperimentJob。
- ExperimentEvalOnlyConfig 使用 single-table inheritance，共用 experiment_jobs，不增加表。
- JSON 配置暂存普通 dict / list；完整配置校验、名称格式校验和版本写入服务后续实现。
- JSON 原地修改需重新赋值属性以确保 SQLAlchemy 跟踪，例如 `row.metadata_ = {**row.metadata_, "key": "value"}`。
- 异步查询需要的关联请显式预加载，如 `selectinload(Experiment.runs)`，避免隐式关系加载触发额外 I/O。

## 已实现的约束

- 原核心链路内部外键及 CASCADE 删除；身份链路采用 RESTRICT 保护引用记录，experiments.user_id 使用 SET NULL 保留实验。
- 数据集名称、外部样本标识、样本修订、任务执行、评分名称和关联表的唯一 / 联合主键约束。
- revision_kind、annotator_kind、评分器和任务类型、后台任务状态的检查约束。
- 评分器类型与子表、任务类型与子表的一致性约束。
- Prompt 任务的 custom_provider_id 与 connection 互斥。
- PostgreSQL 临时实验的 updated_at 部分索引，以及字典中列出的其他索引。
- 实验/重复序号正数、Job 状态、结果有无、有限非负耗时、HTTP 状态范围及时间顺序。
- Attempt 终态的完整结果与接收时间、unknown/rejected 原因、取消原因与时间的一致性。
- Job 队列的 (status, created_at) 索引，以及 Runner/Job 活动 Attempt 的部分唯一索引。
- 用户名 / token_id 唯一、身份名称非空白、auth_version 为正、凭证摘要长度为 32 字节、到期与撤销时间检查；ORM 默认凭证有效期为十个日历年。
- Runner 与凭证一对一：runner_credentials.runner_id 唯一，Python 关系为 Runner.credential；创建服务同时写入二者。
- Runner.deleted_at 为逻辑删除标记，删除后必须 enabled=false；活动列表使用归属 / 创建时间 / ID 的部分索引。删除服务撤销凭证，保留 Attempt 历史。

跨数据集一致性仍需写入服务校验：例如修订的样本和版本属于同一数据集、实验快照的修订属于所选样本。
这些不是 Phoenix 两个独立外键自动保证的条件，本阶段不通过 ORM 隐式写入钩子改变它们。

## 已确认暂缓的外部外键

当前保留以下外部引用字段，尚未补充数据库外键；experiments.user_id 与 runner_attempts.runner_id 已随身份表接入，其余模块保持原样：

| 字段位置 | 未来目标 |
| --- | --- |
| Dataset、DatasetVersion、Evaluator、CodeEvaluatorVersion、DatasetEvaluators 的 user_id | users.id（表已实现，这些旧模块的外键另行接入） |
| DatasetExample.span_rowid | spans.id |
| LLMEvaluator.prompt_id / prompt_version_tag_id | prompts.id / prompt_version_tags.id |
| CodeEvaluator.language / sandbox_config_id | languages.name / sandbox_configs.id |
| CodeEvaluator 的 sandbox_config_id + language | sandbox_configs 的 id + language |
| DatasetEvaluators.project_id | projects.id |
| ExperimentPromptTask.custom_provider_id / prompt_version_id | generative_model_custom_providers.id / prompt_versions.id |

字段的 Column.info、复合约束的 Table.info 保存目标信息，供后续补迁移时参考；它们本身不生成 SQL 约束。
外部字段仍保留原字典的类型、可空性和索引；必填字段暂由调用方提供标识，当前数据库不校验其目标是否存在。
users 为真实账号模型，不是占位表；未创建 Trace、Prompt、沙箱等占位表。

## 验证与迁移

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试契约 `tests/schema_contract.json` 固定了已确认字典的字段名与可空性。
模型专项测试使用内存 SQLite 真正执行建表、写入、查询和约束失败；另行编译全部 PostgreSQL DDL，验证 JSONB、时区字段及复合外键。
这些不替代真实 PostgreSQL 集成测试。

21 张表、183 个字段与字典契约一致；SQLite 验证唯一约束、身份引用、默认有效期及历史保护，PostgreSQL DDL 验证 JSONB、BYTEA、时间类型及部分索引。Runner 接口验证见 [Runner 管理](server-runners.md)。
临时从活动索引移除 unknown 后，容量测试按预期失败；恢复源码后完整测试通过。数据字典的字段名和可空性已逐表核对，编译检查及 git diff --check 通过。

实例就绪后生成迁移：

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "create core tables"
# 审查迁移脚本后执行
.\.venv\Scripts\python.exe -m alembic upgrade head
```

本阶段仅交付 ORM；未生成正式初始迁移，未执行真实 PostgreSQL 建表和读写。

如已有旧数据库，不能直接套用结构变更：需先为历史 Run 回填唯一 job_id 与执行状态，将旧 error 文本转换为结构化 JSON，并处理身份字段的孤立引用，再添加约束；旧配置表继续保留。迁移由实例就绪后的实际数据决定，不通过 create_all 自动升级。
