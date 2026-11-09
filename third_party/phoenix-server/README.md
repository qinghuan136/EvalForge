# Phoenix 服务端代码来源

上游：https://github.com/Arize-ai/phoenix
参考 commit：`14c193cfad309198168e3c2db522db0ec770a557`。
ORM 参考 `src/phoenix/db/models.py` 中的 Dataset、Experiment、Evaluator 和 ExperimentJob 相关模型。

EvalForge 的 17 张核心表参照并改写了这些字段、约束及继承结构；修改包括模块拆分、通用 JSON / PostgreSQL JSONB 类型、ORM 关联整理，以及按项目范围暂缓外部模块外键。
`evalforge/server/evaluators/builtin.py` 移植并修改了上游 `src/phoenix/server/api/evaluators.py` 中五个内置规则的评分逻辑。EvalForge 用 Pydantic 校验已解析的参数，简化部分结果解释，并用本项目的 `EvaluationResult` 返回分数和标签。无效参数、正则或 JSON 通过异常报告。

上游 LICENSE 和 IP_NOTICE 原文保留在本目录，并随 Python 分发包一同发布。
