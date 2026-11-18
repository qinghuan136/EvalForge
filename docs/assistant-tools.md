# 评测助手基础工具

工具名采用 `领域_动作_资源`，领域名大写。详情查询统一使用 `领域_get_资源_detail`。下文定义 17 个工具的输入和模型可见输出，后端路径统一省略 `/api/v1`。

本文对应原生 Deferred Tools 审批实现，执行与数据库存储边界见文末。

- 参数默认必填，`?` 表示可省略。数值型资源 ID 为正整数。`page?: int` 从 `1` 开始，默认 `1`。`pagesize?: int` 默认为 `20`，范围为 `1–100`。`Page<T>` 返回 `{items: T[], page, pagesize, total}`。
- 资源归属由当前登录用户决定。用户 ID、登录凭证和 CSRF Token 由调用层处理。失败沿用后端 `{error: {code, message}}`。
- 查询工具直接返回数据。五个写工具注册 `requires_approval=True`；Run 返回 `DeferredToolRequests`，保存 Conversation 后发送 `approval_required`，数据为 `{revision, cards: [{tool_call_id, tool, title, description, arguments}]}`。
- 网页一次提交全部 `decisions` 后由服务端恢复 Agent，写工具逐个调用业务 Service。拒绝不执行，脱敏结果交给模型继续回答。Conversation 已保存到数据库，刷新或换实例后可恢复提案，未提交的选择清空。

表中 HTTP 路径仅为对应业务入口；所有工具在后端直接调用 Service，网页不再根据工具提案直接请求写业务接口。

## Runner

| 工具 | 参数 | 成功响应 | 后端接口 |
| --- | --- | --- | --- |
| `RUNNER_create_runner` | `name: string`，1–100 字符 | `RunnerCreated`。新签发的凭证和连接地址单独交给用户 | `POST /runners` |
| `RUNNER_delete_runner` | `runner_id: string` | `{runner_id, deleted: true}`，由后端 `204` 转换。已有连接凭证会被撤销 | `DELETE /runners/{runner_id}` |
| `RUNNER_list_runners` | `page?`、`pagesize?` | `Page<RunnerDetail>` | `GET /runners` |
| `RUNNER_get_runner_detail` | `runner_id: string` | `RunnerDetail` | `GET /runners/{runner_id}` |

- `RunnerCreated` 含 `runner_id`、`name`、`enabled`、`created_at`。
- `RunnerDetail` 另含 `updated_at`、`last_seen_at`（可空）和 `connection: {online, phase, capabilities}`。`phase` 为 `disconnected`、`connected`、`syncing` 或 `ready`。

## Dataset

| 工具 | 参数 | 成功响应 | 后端接口 |
| --- | --- | --- | --- |
| `DATASET_import_dataset` | `name: string`（1–100 字符）、`description?: string`（最多 2,000 字符，可空）、`format`（`csv` 或 `jsonl`）、`content: string`。CSV 另传 `input_keys: string[]`，可传 `output_keys?: string[]`、`metadata_keys?: string[]` | `{dataset_id, dataset_version_id, example_count}` | `POST /datasets/import` |
| `DATASET_list_datasets` | `page?`、`pagesize?` | `Page<DatasetSummary>` | `GET /datasets` |
| `DATASET_get_dataset_detail` | `dataset_id: int` | `DatasetSummary` 加 `metadata`、`example_count` | `GET /datasets/{dataset_id}` |
| `DATASET_list_examples` | `dataset_id: int`、`version_id?: int`、`page?`、`pagesize?`。省略版本则取最新版本 | `Page<Example>` 加 `dataset_id`、`dataset_version_id`（可空） | `GET /datasets/{dataset_id}/examples` |

- `DatasetSummary` 含 `dataset_id`、`name`、`description`、`latest_version_id`、`created_at`、`updated_at`。描述和最新版本 ID 可空。
- `Example` 含 `example_id`、`input`、`output`（参考答案）、`metadata`，后三项为 JSON 对象。
- 导入工具把 `content` 编码成 UTF-8 后作为上传文件提交，最多 5 MiB、10,000 条样例。CSV 至少选一个输入列，三个列选择列表之间不能重复。JSONL 每行提供 `input`，可附 `output` 和 `metadata`，无需选列。[文件格式](api/数据集导入.md)

## Experiment

| 工具 | 参数 | 成功响应 | 后端接口 |
| --- | --- | --- | --- |
| `EXPERIMENT_create_experiment` | `dataset_id: int` 和下列创建字段 | `ExperimentDetail` | `POST /runners/{runner_id}/datasets/{dataset_id}/experiments` |
| `EXPERIMENT_list_experiments` | `dataset_id: int`、`page?`、`pagesize?` | `Page<ExperimentSummary>` | `GET /datasets/{dataset_id}/experiments` |
| `EXPERIMENT_get_experiment_detail` | `experiment_id: int` | `ExperimentDetail` | `GET /experiments/{experiment_id}` |
| `EXPERIMENT_cancel_experiment` | `experiment_id: int` | `ExperimentDetail`。`cancelling` 表示取消已受理，仍有任务待结束 | `POST /experiments/{experiment_id}/cancel` |

创建字段包括 `name: string`（1–100 字符）和 `execution_config`。后者要求 `runner_id: string`、`agent_target_id: string`，可选 `timeout_seconds`（默认 60，范围 `(0, 3600]`）和 `stream_output`（默认 `false`）。路径中的 `runner_id` 取自 `execution_config.runner_id`。

工具调用必须提供 `dataset_version_id: int`，先查询数据集得到版本，确保审批参数与实际执行版本一致；普通 REST 创建接口仍允许省略版本。可选字段包括 `description`（最多 2,000 字符，可空）、`repetitions: int`（默认 1，范围 1–100）、`metadata`（JSON 对象，默认 `{}`）。样例数乘重复次数最多 10,000。

可传 `dataset_evaluator_ids?: int[]` 或 `evaluators?: EvaluatorConfig[]`，两者默认 `[]`，各最多 100 项，不能同时非空。评分配置含 `name`、`evaluator_key`、`description?`、`input_mapping`（`path_mapping?` 和 `literal_mapping?`）。[创建字段与评分配置](api/实验创建与查询.md#1-创建实验)

- `ExperimentSummary` 含 `experiment_id`、`dataset_id`、`dataset_version_id`、`name`、`status`、`repetitions`、`created_at`、`updated_at` 和 `progress`。进度含 `example_count`、`job_count` 及各 Job 状态的计数。
- 实验 `status` 为 `pending`、`running`、`cancelling`、`completed`、`failed` 或 `cancelled`。
- `ExperimentDetail` 另含 `description`、`metadata`、`execution_config`、`evaluators`（评分配置快照），以及 `progress.evaluations: {expected, completed, errors}`。

## Job

| 工具 | 参数 | 成功响应 | 后端接口 |
| --- | --- | --- | --- |
| `JOB_list_jobs` | `experiment_id: int`、`status?: JobStatus`、`page?`、`pagesize?` | `Page<JobSummary>` | `GET /experiments/{experiment_id}/jobs` |
| `JOB_get_job_detail` | `experiment_id: int`、`job_id: string` | `JobDetail` | `GET /experiments/{experiment_id}/jobs/{job_id}` |

- `JobStatus` 为 `queued`、`assigned`、`running`、`succeeded`、`failed`、`cancelled` 或 `unknown`。
- `JobSummary` 含 `job_id`、`example_id`、`repetition_number`、`status`、`duration_ms`、`http_status`、`evaluation_progress: {expected, completed, errors}`。
- `JobDetail` 另含 `input`、`reference`、`metadata`、`output`（任意 JSON 值）、`error`（可空，非空时含 `code` 和 `message`）、`started_at`、`ended_at`、`trace_id`，以及 `evaluations`（每项含 `name`、`score`、`label`、`explanation`、`error`）。`trace_id` 可空，有值时可继续查 Trace。

## Trace

| 工具 | 参数 | 成功响应 | 后端接口 |
| --- | --- | --- | --- |
| `TRACE_list_traces` | `page?`、`pagesize?`、`trace_id?: string`（精确筛选） | `Page<TraceSummary>` | `GET /traces` |
| `TRACE_get_trace_detail` | `trace_id: string`、`after_span_id?: string`、`limit?: int`（默认 250，范围 1–500） | `TraceSummary` 加 `spans: SpanSummary[]`、`next_span_id`（可空） | `GET /traces/{trace_id}` |
| `TRACE_get_span_detail` | `trace_id: string`、`span_id: string` | `SpanDetail` | `GET /traces/{trace_id}/spans/{span_id}` |

- Trace ID 和 Span ID 分别为 32 位和 16 位小写十六进制字符串。把 `next_span_id` 作为下一次请求的 `after_span_id`，值为 `null` 时已读完。
- `TraceSummary` 含 `trace_id`、`title`、`service_name`、`root_span_id`、`status_code` 和起止时间。
- `SpanSummary` 含 `span_id`、`parent_span_id`、`name`、`kind`、`status_code` 和起止时间。起止时间字段为 `start_time_unix_nano` 和 `end_time_unix_nano`，均是十进制字符串。
- `SpanDetail` 另含 `trace_id`、`status_message`、`trace_state`、`flags`、`attributes`、`events`、`links`、`resource`、`instrumentation_scope`、`dropped_attributes_count`、`dropped_events_count`、`dropped_links_count`。[Trace 查询接口](api/Trace查询.md)

## Deferred Tools 执行约定

完整约定见 [审批与持久化方案](AgentDeferredTools_审批与持久化方案.md)。17 个工具的业务能力继续保留，主要调整执行方式：

- 查询工具正常执行；创建 Runner、删除 Runner、导入数据集、创建实验、取消实验五个工具注册 `requires_approval=True`，批准后由工具体调用对应业务 Service。
- 已移除手写 `_propose()`、`requires_confirmation` 和业务 `action_id`。待审批参数由数据库中的完整 Conversation 提供，卡片使用 `tool_call_id`。
- 前端收齐全部决定后提交 `decisions: {tool_call_id: boolean}`；后端校验归属、版本和决定集合后恢复 Agent，前端不直接请求表中的写业务接口。
- 工具执行结果脱敏后进入模型上下文；业务 Service 仍负责权限及实时条件校验。参数校验与必要的版本固定不能因审批改造而遗漏。
- ConversationStore 保存完整历史和待审批请求，支持跨实例和重启恢复。逐项选择在统一提交前留在前端。
- Runner 创建结果向模型返回摘要，完整凭证通过当前 SSE 的 `display` 卡片交付，详见 [事件与卡片协议](assistant-sse.md)。卡片保存在当前页面内存，刷新后丢失。

五个写工具可传可选的 `approval_description`（最多 500 字符）说明操作目的。该字段作为批准卡片的描述，业务服务只接收其余业务参数。省略时由后端生成默认描述。
