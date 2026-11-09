# ExperimentRuntime 独立实现与接入边界

`ExperimentRuntime` 是单个实验的进程内对象，现阶段可独立构造。它保存实验 ID、Runner 执行参数、评分配置快照和两个待办缓冲。当前 Scheduler、会话层和数据库服务尚未使用这个对象。 [实现](../evalforge/server/experiment_runtime.py)

| 缓冲 | 存什么 | 什么时候加入 |
| --- | --- | --- |
| 待 Task | `ExperimentRun.id` | 调用方读到可派发的 Job 后加入 |
| 待 Eval | `EvalWorkItem(experiment_run_id, evaluator_name)` | Task 成功结果提交后，按该实验选中的评分配置加入 |

两个缓冲都按先进先出取项。相同工作在等待期间重复加入时只保留一份，位置保持原样。恢复时可通过 `completed_names` 跳过已保存评分结果的指标，包括已经记录了评分错误的指标。`pending_tasks` 和 `pending_evals` 仅统计当前缓冲里的条数。扫描游标、执行中的工作及实验总进度由后续接入方定义。

## 接入时需要协调的地方

| 位置 | 当前链路 | 接入时的边界 |
| --- | --- | --- |
| Task 领取 | `DispatchService.next_action()` 按 Runner 查找 `queued` Job，在事务中领取并创建 Attempt | Runtime 里的 Task ID 只作为候选。出队后仍需在数据库校验 Job 状态、实验状态和 Runner 容量。接入时需统一候选的补充来源，避免内存队列和现有扫描各自派发 |
| 评分待办 | 实验保存了配置快照，评分执行链还待接入 | Task 成功提交后再入队。恢复时用成功的 TaskRun、评分快照和已有 annotation 重建缓冲。按 `(experiment_run_id, name)` 保存结果，内存去重只覆盖仍在队列中的工作 |
| 完成与取消 | 当前 `finish_experiment()` 按 Task Job 状态收尾 | 正常完成需要同时检查数据库里的 Task 和评分结果。队列为空时可能仍有执行中的工作。取消后停止领取新工作，清理缓冲，并在提交评分前检查实验状态 |
| 实例与生命周期 | 当前 Scheduler 只处理本进程持有的 Runner 会话 | 明确由哪个调度组件创建、补充和释放 Runtime。多实例之间的领取、归属和幂等仍由持久化状态协调 |

[当前派发逻辑](../evalforge/server/services/dispatch.py#L61) · [当前收尾逻辑](../evalforge/server/services/attempts.py#L50) · [原架构规划](云端运行架构.md)

### 已确认的配置冲突

实验创建会把评分配置存到 `experiments.execution_config["evaluators"]`。当前 `DispatchService` 用只声明 Runner 参数的 `ExecutionConfig` 校验整个对象，额外的 `evaluators` 会触发 `extra_forbidden`，使 Job 按 `invalid_execution_config` 失败。接入前需要分别解析 Runner 执行参数与评分配置快照，或为数据库存储单独定义一个包含两者的模型。`ExperimentRuntime` 已将二者分为独立字段，具体如何从存储构造它留给接入阶段。

[保存快照](../evalforge/server/services/experiments.py#L96) · [派发校验](../evalforge/server/services/dispatch.py#L98) · [Runner 参数定义](../evalforge/server/schemas/experiments.py#L17)
