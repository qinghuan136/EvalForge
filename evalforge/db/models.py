"""ORM 统一入口；导入后，21 张表全部注册到 Base.metadata。

内存数据契约继续位于 evalforge/models.py。Job 复用 ExperimentRun；RunnerAttempt
保存云端派发尝试；User / Runner / RunnerCredential 保存身份与凭证。Result 不另建表。
"""

from .base import Base
from .orm.attempts import RunnerAttempt
from .orm.auth import Runner, RunnerCredential, User
from .orm.datasets import Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion
from .orm.evaluators import (
    BuiltinEvaluator, CodeEvaluator, CodeEvaluatorVersion, DatasetEvaluators, Evaluator,
    LLMEvaluator,
)
from .orm.experiments import (
    Experiment, ExperimentDatasetExample, ExperimentRun, ExperimentRunAnnotation,
)
from .orm.jobs import (
    ExperimentDatasetEvaluator, ExperimentEvalOnlyConfig, ExperimentJob, ExperimentPromptTask,
)

__all__ = [
    "Base", "Dataset", "DatasetVersion", "DatasetExample", "DatasetExampleRevision",
    "Experiment", "ExperimentDatasetExample", "ExperimentRun", "ExperimentRunAnnotation",
    "Evaluator", "CodeEvaluator", "CodeEvaluatorVersion", "LLMEvaluator", "BuiltinEvaluator",
    "DatasetEvaluators", "ExperimentJob", "ExperimentPromptTask", "ExperimentEvalOnlyConfig",
    "ExperimentDatasetEvaluator", "RunnerAttempt", "User", "Runner", "RunnerCredential",
]
