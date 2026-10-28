# Phoenix Experiment 链路研究与 EvalForge V0 开发记录

参考版本：本地 Phoenix commit `14c193cfad309198168e3c2db522db0ec770a557`，
主包 20.26.1，phoenix-client 包 3.5.0。本文定位均以此快照为准。
下文路径相对于旁边的 Phoenix 仓库根目录；不是网络最新版文档。

## 1. 选择研究哪条执行链路

本次研究 `phoenix.client.experiments.run_experiment` 所代表的 Client 执行链路。
用户传入的 Python Task 和 Evaluator 在调用 Client 的进程中执行。
服务端的实验后台 Runner 是另一条链路，本阶段不展开。

```text
Client.datasets.get_dataset(...)
  → resources/datasets.Dataset.examples
  → phoenix.client.experiments.run_experiment(...)
  → Client.experiments.run_experiment(...) / Experiments.run_experiment
      → 校验 task 签名、dataset、repetitions
      → 创建实验记录（非 dry_run）
      → 展开 TestCase(example, repetition_number)
      → SyncExecutor.run(test_cases)
          → Experiments._run_single_task_sync
              → _bind_task_signature → task(...)
              → ExperimentRun（output / error / example_id）
              → 提交运行记录（非 dry_run）
      → 组装 RanExperiment.task_runs
      → Experiments.evaluate_experiment
          → _evaluators_by_name → create_evaluator
          → 获取对应版本 Dataset
          → _build_evaluation_tasks（run × evaluator，并关联 example）
          → _run_evaluations → SyncExecutor.run
              → _run_single_evaluation_sync
                  → evaluator.evaluate(output, expected, input, metadata, ...)
                  → _bind_evaluator_signature → 用户评分函数
                  → _default_eval_scorer
                  → ExperimentEvaluationRun（result / error）
                  → 提交评分记录（非 dry_run）
      → RanExperiment.task_runs + evaluation_runs
```

`Dataset.examples` 已包含本次数据集版本的样本。原版 evaluate_experiment 会再次取版本化样本，
用 example ID 关联 TaskRun，避免将不同样本的预期答案和输出错配。
原版还区分 node_id 与 id 的历史格式；V0 只使用本地唯一字符串 id。

## 2. 核心文件、类与函数

| 源码 | 核心符号 | 读它要理解什么 |
| --- | --- | --- |
| `packages/phoenix-client/src/phoenix/client/resources/datasets/__init__.py` | `Datasets.get_dataset`、`Dataset`、`Dataset.examples` | 从 API 读取数据集和版本化样本；Dataset 是数据容器 |
| `packages/phoenix-client/src/phoenix/client/experiments/__init__.py` | `run_experiment` | 便捷入口，实际委托给 Client 的资源对象 |
| `packages/phoenix-client/src/phoenix/client/resources/experiments/__init__.py` | `Experiments.run_experiment` | 先运行 Task，再运行 Evaluator 的总编排 |
| 同上 | `_validate_task_signature`、`_bind_task_signature` | 按参数名传入 input、参考答案、metadata；深拷贝隔离回调 |
| 同上 | `_run_single_task_sync` | 调用 Task，记录 output/error，并关联样本 |
| 同上 | `evaluate_experiment`、`_build_evaluation_tasks`、`_run_evaluations` | 统一评分器、关联样本、展开 run × evaluator |
| 同上 | `_run_single_evaluation_sync` | 将 Task 输出和样本传给评分器，生成评分执行记录 |
| `packages/phoenix-client/src/phoenix/client/resources/experiments/evaluators.py` | `create_evaluator`、`_bind_evaluator_signature`、`_default_eval_scorer` | 普通函数适配为评分器，并统一返回形式 |
| `packages/phoenix-client/src/phoenix/client/resources/experiments/types.py` | `TestCase`、`ExperimentEvaluationRun`、`RanExperiment` | 任务调度单元、评分记录、实验结果容器 |
| `packages/phoenix-client/src/phoenix/client/utils/executors.py` | `SyncExecutor.run` | 顺序执行、重试、失败处理和进度统计；V0 只保留顺序循环与局部失败隔离 |
| `packages/phoenix-client/tests/client/resources/experiments/test_default_eval_scorer.py` | 返回值契约测试 | bool→score+label；二元组是 score+label，三元组才含 explanation |

## 3. 选择性借鉴与对应代码

| 成熟设计 | EvalForge 落点 | 本阶段精简 |
| --- | --- | --- |
| 数据集与 Task 分离 | `models.py`、`dataset.py` | 本地 JSON 读取，预期值命名 expected，不含版本服务 |
| 按函数签名绑定参数 | `runner.py` 的 `_prepare / _bind / _invoke` | 同步、固定参数名和单参回退，不引入完整反射适配体系 |
| 输入、参考答案与输出隔离 | 每次绑定均 deepcopy；Task 输出保存副本 | 防止一个评分器修改下一评分器看到的数据 |
| TaskRun 与评分记录分离 | `TaskRun / EvaluationRun / ExperimentResult` | 以 example_id 关联；每条样本只执行一次 |
| 两阶段执行 | `run_experiment` 中 Task 循环与 Evaluator 循环 | 不加入通用执行器、重试、队列或并发 |
| 评分返回值规范化 | `normalize_evaluation` | 保留常见单结果类型，不支持 Score SDK 对象、多结果列表 |
| 错误作为结果的一部分 | task error、evaluation error、skipped | 不依赖服务端记录，也不把异常混成低分 |
| 结果对象再交给输出层 | `reporting.py` | JSON 文件与终端摘要，均分只统计有效数值 |

V0 是参照这些设计的本地精简改写，不是直接调用或原样移植 Phoenix Runner。
没有 Phoenix import，也没有 arize 包依赖；不再复制 Phoenix 的 API、Tracing、生成类型和服务端配置。

## 4. 研究中遇到的问题与处理

- **dry_run 不是纯离线模式。** 原版 evaluate_experiment 即使 dry_run 仍请求数据集，
  因此 V0 直接持有样本快照，不通过 dry_run 绕开服务端。
- **原版数据集 output 与任务 output 容易混淆。** 前者是参考答案，V0 明确用 expected；
  仍保留 reference 参数别名以便对照学习。
- **评分器有多种返回形式。** 参照原版规范化契约，尤其保留二元组第二项为 label 的语义。
- **原版 Task 错误会写入服务端并重新抛给执行器，触发重试。** V0 直接保留错误并继续，
  将对应评分显式标为 skipped，不复刻原版重试与远端状态回读。
- **局部错误与零分含义不同。** V0 保留合法零分；评分异常、跳过、纯标签不进入均分分母。
- **同步实现不能悄悄接收 async Task。** 签名前检拒绝已知协程函数，运行时也检查返回 awaitable，
  关闭未执行的原生 coroutine 并记错，避免输出假的成功结果。
- **JSON 数据与结果必须可落盘。** 拒绝 NaN/Infinity 等非有限值；非 JSON Task 输出按 Task 错误处理。
  测试发现只用 parse_constant 不能拦截 `1e999` 溢出，所以在读取后增加严格 JSON 序列化检查。
- **Windows 文件输入可能带 UTF-8 BOM。** 数据集读取使用 utf-8-sig。

## 5. 范围和验证

只实现读取 JSON、单次同步 Task、多个同步 Evaluator、结构化结果与报告。
不实现前端、数据库、API、并发、异步、超时、重试、Agent、Tracing 或实验恢复。

验证命令：

```sh
python -m unittest discover -s tests -v
python -m examples.run_demo
```

测试覆盖本地闭环、两阶段顺序、参数绑定、输入输出隔离、Task 错误与评分错误隔离、
跳过与零分、纯标签均分、异步拒绝、数据校验、JSON 报告以及真实 CLI 调用。
2026-10-28 实际验证：

| 项目 | 结果 |
| --- | --- |
| Python | 内置 CPython 3.12.14；核心仅要求 3.11+，没有安装运行依赖 |
| 自动化测试 | `python -X utf8 -W error -m unittest discover -s tests -v`，21 项全部通过 |
| 完整示例 | `python -X utf8 -m examples.run_demo`，退出码 0 |
| Task | 4 条全部执行成功，0 条执行异常 |
| exact_match | 4 条评分，均分 0.75；最后一条故意不匹配，为合法 0 分 |
| non_empty | 4 条评分，均分 1.0 |
| 输出报告 | `results/demo.json`，包含输入、参考答案、实际输出、逐项评分和摘要 |

首次测试因 Windows 沙箱临时目录不可写而受阻，经授权在沙箱外完成测试。
随后修复上述数值溢出问题并重跑，通过全部测试。Phoenix 源码未修改。

## 6. 上游来源记录

来源仓库：https://github.com/Arize-ai/phoenix ，固定到上述 commit。
重点参考 phoenix-client 的实验资源实现和类型契约。
上游包 LICENSE、IP_NOTICE 以及根 LICENSE 原文保存在 `third_party/phoenix-client/`。
它们保留本地上游各自的原始声明；本笔记不对不同文件的许可声明作重新解释。
