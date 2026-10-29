# 核心 ORM

参考 Phoenix commit `14c193cfa` 和已确认的数据字典，实现 17 张表、132 个字段。
这里定义存储结构；尚未连接 PostgreSQL、执行建表或把 V0 Runner 接入数据库。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| `evalforge/db/base.py` | ORM 基类、JSONB / 时区时间类型、约束命名、暂缓外键标记 |
| `evalforge/db/orm/datasets.py` | Dataset、Version、Example、Revision |
| `evalforge/db/orm/experiments.py` | Experiment、样本快照关联、TaskRun、EvalRun |
| `evalforge/db/orm/evaluators.py` | 评分器公共定义、三个子类型、源码版本、数据集使用配置 |
| `evalforge/db/orm/jobs.py` | 服务端实验配置、Prompt Task、EVAL_ONLY、评分配置关联 |
| `evalforge/db/models.py` | 统一导入和注册入口，Alembic 由此获得完整 metadata |

```python
from evalforge.db.models import Dataset, DatasetVersion, ExperimentRun

dataset = Dataset(name="价格测试集", metadata_={"purpose": "regression"})
version = DatasetVersion(dataset=dataset, description="初始版本")
# 由 AsyncSession 的 add / flush / commit 完成保存；创建 Python 对象本身不写数据库。
```

现有 `evalforge/models.py` 继续保留 dataclass，不改动 V0 的 Task / Evaluator 接口。
`TaskRun` 在 ORM 中对应 `ExperimentRun`，`EvalRun` 对应 `ExperimentRunAnnotation`。
Result 和 Runner 不建独立表。

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

- 全部核心表内部外键及 CASCADE 删除。
- 数据集名称、外部样本标识、样本修订、任务执行、评分名称和关联表的唯一 / 联合主键约束。
- revision_kind、annotator_kind、评分器和任务类型、后台任务状态的检查约束。
- 评分器类型与子表、任务类型与子表的一致性约束。
- Prompt 任务的 custom_provider_id 与 connection 互斥。
- PostgreSQL 临时实验的 updated_at 部分索引，以及字典中列出的其他索引。

跨数据集一致性仍需写入服务校验：例如修订的样本和版本属于同一数据集、实验快照的修订属于所选样本。
这些不是 Phoenix 两个独立外键自动保证的条件，本阶段不通过 ORM 隐式写入钩子改变它们。

## 已确认暂缓的外部外键

用户选择本次只实现核心表，保留以下字段，但不建立指向未实现模块的数据库外键：

| 字段位置 | 未来目标 |
| --- | --- |
| Dataset、DatasetVersion、Experiment、Evaluator、CodeEvaluatorVersion、DatasetEvaluators 的 user_id | users.id |
| DatasetExample.span_rowid | spans.id |
| LLMEvaluator.prompt_id / prompt_version_tag_id | prompts.id / prompt_version_tags.id |
| CodeEvaluator.language / sandbox_config_id | languages.name / sandbox_configs.id |
| CodeEvaluator 的 sandbox_config_id + language | sandbox_configs 的 id + language |
| DatasetEvaluators.project_id | projects.id |
| ExperimentPromptTask.custom_provider_id / prompt_version_id | generative_model_custom_providers.id / prompt_versions.id |

字段的 Column.info、复合约束的 Table.info 保存目标信息，供后续补迁移时参考；它们本身不生成 SQL 约束。
外部字段仍保留原字典的类型、可空性和索引；必填字段暂由调用方提供标识，当前数据库不校验其目标是否存在。
没有创建虚假的用户、Trace、Prompt、沙箱等占位表。

## 验证与迁移

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试契约 `tests/schema_contract.json` 固定了已确认字典的字段名与可空性。
模型专项测试使用内存 SQLite 真正执行建表、写入、查询和约束失败；另行编译全部 PostgreSQL DDL，验证 JSONB、时区字段及复合外键。
这些不替代真实 PostgreSQL 集成测试。

2026-10-29 实测：47 项测试中 46 项通过，1 项因没有 PostgreSQL 实例而跳过；其中 14 项为新增模型专项测试。17 张表、132 个字段的名称和可空性均与已确认字典一致，所有内部外键均能解析。

实例就绪后生成迁移：

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "create core tables"
# 审查迁移脚本后执行
.\.venv\Scripts\python.exe -m alembic upgrade head
```

本阶段仅交付 ORM；未生成正式初始迁移，未执行真实 PostgreSQL 建表和读写。
