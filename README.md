# EvalForge V0

本地 JSON Dataset → Experiment Runner → Task → Evaluator → 终端摘要 / JSON 报告。

Python 3.11+。V0 离线实验执行仅使用标准库，无需启动 Phoenix、数据库或模型服务。
项目已另外安装 PostgreSQL 基础设施（SQLAlchemy + asyncpg + Alembic），使用方式见 [数据库说明](docs/database.md)。
云端 Redis 异步连接基础设施已就绪（redis-py），配置、资源管理与检查命令见 [Redis 说明](docs/redis.md)。
当前 21 张表已实现 ORM（含停用保留的旧配置），云端主链为 Experiment → Job → RunnerAttempt；每个 Job 的评分保存为 JobEvaluation；结构和迁移边界见 [ORM 说明](docs/orm.md)。
新增用户、Runner 与凭证三张表，字段及 Mermaid ER 图见 [身份与凭证设计](docs/auth-schema.md)。
云端七个浏览器鉴权接口与五个 Runner 管理接口已实现，见 [鉴权服务](docs/server-auth.md)与 [Runner 管理](docs/server-runners.md)。[WSS / JSON-RPC 接入](docs/server-wss.md)已接通认证、会话、Attempt 同步与结果上报。
对象关系、协程、消息流及存储边界见 [当前项目架构图解](docs/当前项目架构图解.md)（内含 Mermaid 图）。
登录鉴权与 Runner 管理接口约定见 [接口文档](docs/api/登录鉴权与Runner管理.md)。
实验创建、列表和详情已实现。创建时固定版本快照并生成待执行 Job，进程级 Scheduler 会自动派发给已就绪且有空闲容量的指定 Runner。见 [实验接口](docs/api/实验创建与查询.md)和 [当前架构](docs/当前项目架构图解.md#10-进程级调度)。

独立的本地客户端位于 [client/](client/README.md)，已实现 WebSocket 传输、双向 JSON-RPC 和本地 SQLite 存储基础层，
单独安装依赖。已接通会话同步、单 Job HTTP 执行、结果持久化与确认上报，见 [Job 执行链路](docs/client/Job执行层核心链路.md)。

## 运行

在 EvalForge 项目根目录执行（PowerShell）：

先看占位函数串起的标准流程（无需数据库）：

```powershell
.\.venv\Scripts\python.exe -m examples.run_pipeline
```

生成 `results/pipeline.json`：包含一次 Experiment、4 条 TaskRun、4 条 EvaluationRun 和摘要。
占位 Task 透传输入，Evaluator 只返回 `placeholder` 标签；均分为 `null`，不代表质量评分。
后续替换 `examples/run_pipeline.py` 中的两个普通函数即可，Runner 不需要改动。
流程结构和源码阅读说明见 [内存执行流程](docs/runner-pipeline.md)。

数据库相关命令和全部测试请使用项目的 `.venv` 解释器；V0 示例也可继续独立运行。

```powershell
.\.venv\Scripts\python.exe -m examples.run_demo
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

示例默认生成 `results/demo.json`。4 条文本规范化样本全部执行成功，
其中 1 条故意要求 Task 不具备的音译能力：`exact_match` 均分 0.75，`non_empty` 均分 1.0。
**执行成功不等于答案正确**：错误答案是 0 分，执行异常另记 error。

```sh
python -m examples.run_demo --dataset examples/text_cases.json --output results/my-run.json
```

V0 示例可直接从源码运行；数据库部分需要先安装项目依赖，见数据库说明。

如果本机尚未配置系统 Python，本次验证使用的内置解释器也可以直接执行（PowerShell）：

```powershell
& 'C:\Users\30233\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -X utf8 -m examples.run_demo
```

## 换成自己的 Task

```python
from evalforge import load_dataset, run_experiment, write_report

def my_task(input):
    return input["question"].strip()

def correctness(output, expected):
    return output == expected

result = run_experiment(
    dataset=load_dataset("my-cases.json"),
    task=my_task,
    evaluators={"correctness": correctness},
    experiment_name="first-experiment",
)
write_report(result, "results/first-experiment.json")
```

数据集是 UTF-8 JSON 数组，每条格式如下。id 必须唯一且非空，input 和 metadata 是对象；
expected 可为任意 JSON 值，metadata 可省略，expected 为 null 也有效。

```json
[
  {"id": "case-1", "input": {"question": " hello "}, "expected": "hello", "metadata": {"scenario": "trim"}}
]
```

Phoenix Dataset 的 `output` 存的是参考答案。V0 将它明确命名为 `expected`；
Task 返回值才叫 `output`。这不是对 Phoenix 数据集导出文件的直接兼容读取器。

## 函数契约

- Task 可按需声明 `input / expected / reference / metadata / example`。
- Evaluator 可按需声明上述参数及 `output`；reference 是 expected 的别名。
- 单个任意名称的参数接收 input（Task）或 output（Evaluator）。支持关键字专用、位置专用和默认参数。
- example 是本地 Example 对象，如 `example.id`、`example.expected`，不提供 Phoenix ExampleProxy 的兼容字典接口。
- 函数调用时获得深拷贝，修改参数不影响数据集、已记录输出或其他评分器。
- 评分器返回 bool、数值、字符串、`(score, label)`、`(score, label, explanation)`、包含对应字段的 dict 或 EvaluationResult。
- 分数必须是有限数值，不强制限制在 0～1；标签评分不被自动转换成数值。
- V0 只接受同步函数，不支持 `*args / **kwargs`、多输出评分器、装饰器注册或远程配置。

## 执行和错误行为

1. 预检所有函数签名，再拍下数据集快照。
2. 顺序执行所有 Task，保留每条 input、expected、metadata、output、error。
3. 对每条成功输出执行每个 Evaluator，以 task_run_id 关联具体执行记录，以 example_id 找到样本。
4. Task 失败时，其评分记录为 skipped；评分失败时仅记录当前 evaluator 的 error，继续其余评分。
5. 数值平均分只统计成功的数值评分；0 分参与平均，错误、跳过、纯标签均不补 0。

配置错误直接报错；普通 Task/Evaluator 异常写入实验报告；KeyboardInterrupt/SystemExit 继续向外传播。
每次调用创建独立的 Experiment ID；同名调用也代表两次评测。报告包含 `experiment` 对象，
TaskRun 带 `experiment_id`，EvaluationRun 带 `task_run_id`。这些 ID 是内存记录的 UUID，和 ORM 主键分开。
本地 Runner 没有超时、中断恢复、重试、异步、并发、UI、数据库访问、API、Agent 或 Tracing。

## 建议阅读顺序

1. `examples/text_cases.json` 与 `examples/run_demo.py`：先看输入和调用方式。
2. `evalforge/models.py`：看样本、执行记录与评分记录的契约。
3. `evalforge/runner.py`：看 run_experiment 的两个循环，然后看参数绑定。
4. `evalforge/evaluators.py`：看任意评分返回值怎样统一。
5. `evalforge/reporting.py` 与 `results/demo.json`：看结果关联与均分分母。
6. `docs/phoenix-experiment-chain.md`：逐项对照原版实现。

源码研究、取舍与开发验证记录见 [Phoenix 链路笔记](docs/phoenix-experiment-chain.md)。
