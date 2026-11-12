# ExperimentRuntime 独立实现与接入边界

`ExperimentRuntime` 是单个实验的进程内对象，现阶段可独立构造。它保存实验 ID、Runner 执行参数、评分配置快照和两个待办缓冲。当前 Scheduler、会话层和数据库服务尚未使用这个对象。 [实现](../evalforge/server/experiment_runtime.py)

下一步如何接入，见 [ExperimentRuntime 改造计划（临时）](ExperimentRuntime改造计划（临时）.md)。

| 缓冲 | 存什么 | 什么时候加入 |
| --- | --- | --- |
| 待 Task | `Job.id` | 调用方读到可派发的 Job 后加入 |
| 待 Eval | `EvaluationWorkKey(job_pk, evaluator_name)` | Task 成功结果提交后，按该实验选中的评分配置加入 |

`EvaluationWorkKey` 只定位一项评分；现有评分协程接收的 `EvaluationWorkItem` 还带配置和输入。两个缓冲都按先进先出取项。相同工作在等待期间重复加入时只保留一份，位置保持原样。恢复时可通过 `completed_names` 跳过已保存评分结果的指标，包括已经记录了评分错误的指标。`pending_tasks` 和 `pending_evals` 仅统计当前缓冲里的条数。扫描游标、执行中的工作及实验总进度由后续接入方定义。

## 接入时需要协调的地方

| 位置 | 当前链路 | 接入时的边界 |
| --- | --- | --- |
| Task 领取 | `DispatchService.next_action()` 按 Runner 查找 `queued` Job，在事务中领取并创建 Attempt | 计划用 `JobWorkItem` 缓存待派发 Job 的标识和输入，由补货协程分批加载。Scheduler 只选候选；派发服务在数据库事务中确认状态和容量、创建 Attempt。当前尚未定义 `JobWorkItem` |
| 评分待办 | Scheduler 收到成功的 `job_pk` 后，查库并启动缺失的评分项 | 若未来引入 Runtime 的 Eval 缓冲，仍可从成功的 Job、评分快照和已有 JobEvaluation 重建 |
| 完成与取消 | 由数据库里的 Task 状态与评分记录判断是否完成，保存评分前校验取消状态 | Runtime 未来若参与收尾，仍需区分本地缓冲为空、存在在途工作和数据库已收尾 |
| 实例与生命周期 | 当前 Scheduler 只处理本进程持有的 Runner 会话 | 明确由哪个调度组件创建、补充和释放 Runtime。多实例之间的领取、归属和幂等仍由持久化状态协调 |

[当前派发逻辑](../evalforge/server/services/dispatch.py#L61) · [当前收尾逻辑](../evalforge/server/services/attempts.py#L50) · [原架构规划](云端运行架构.md)

### 执行配置与评分快照

实验创建会把评分配置存到 `experiments.execution_config["evaluators"]`。派发校验 Runner 配置时会拆出这个字段，评分服务单独读取它。`ExperimentRuntime` 同样把两类配置分开保存。当前评分执行链见 [云端评分链路](server-evaluations.md)。

[保存快照](../evalforge/server/services/experiments.py#L96) · [派发校验](../evalforge/server/services/dispatch.py#L98) · [Runner 参数定义](../evalforge/server/schemas/experiments.py#L17)
