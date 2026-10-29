# Phoenix ORM 来源

上游：https://github.com/Arize-ai/phoenix
参考 commit：`14c193cfad309198168e3c2db522db0ec770a557`。
参考文件：`src/phoenix/db/models.py` 中的 Dataset、Experiment、Evaluator、ExperimentJob 相关模型。

EvalForge 的 17 张核心表参照并改写了这些字段、约束及继承结构；修改包括模块拆分、通用 JSON / PostgreSQL JSONB 类型、ORM 关联整理，以及按项目范围暂缓外部模块外键。
未引入上游完整模块、执行器或业务服务。上游 LICENSE 和 IP_NOTICE 原文保留在本目录。
