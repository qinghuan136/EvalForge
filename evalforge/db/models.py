"""ORM 统一入口；导入后，各业务表注册到 Base.metadata。

内存数据契约继续位于 evalforge/models.py。Job 保存样本的一次执行；RunnerAttempt
保存云端派发尝试；User / Runner / RunnerCredential 保存身份与凭证。Result 不另建表。
"""

from .base import Base
from .orm.assistant import AssistantConversationEntry, AssistantConversationRecord, AssistantMessage, AssistantSession
from .orm.attempts import RunnerAttempt
from .orm.auth import Runner, RunnerCredential, User
from .orm.datasets import Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion
from .orm.evaluators import (
    BuiltinEvaluator, CodeEvaluator, CodeEvaluatorVersion, DatasetEvaluators, Evaluator,
    LLMEvaluator,
)
from .orm.experiments import Experiment, ExperimentDatasetExample, Job, JobEvaluation
from .orm.experiment_configs import ExperimentConfig
from .orm.traces import Span, Trace

__all__ = [
    "Base", "AssistantMessage", "AssistantSession", "Dataset", "DatasetVersion", "DatasetExample", "DatasetExampleRevision",
    "Experiment", "ExperimentDatasetExample", "Job", "JobEvaluation",
    "Evaluator", "CodeEvaluator", "CodeEvaluatorVersion", "LLMEvaluator", "BuiltinEvaluator",
    "DatasetEvaluators", "ExperimentConfig", "RunnerAttempt", "User", "Runner", "RunnerCredential",
    "Trace", "Span",
    "AssistantConversationRecord", "AssistantConversationEntry",
]
