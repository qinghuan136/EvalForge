"""Discover and execute missing scores against the experiment's saved snapshots."""

from datetime import datetime, timezone

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from evalforge.db import Database
from evalforge.db.models import CodeEvaluator, CodeEvaluatorVersion, Experiment, Job, JobEvaluation
from evalforge.models import EvaluationResult

from ..errors import APIError
from ..evaluators.execution import evaluate_builtin
from ..evaluators.input_mapping import resolve_inputs
from ..evaluators.snekbox import SnekboxClient
from ..experiment_runtime import EvaluationWorkItem
from ..schemas.code_evaluators import CodeEvaluatorContext
from ..schemas.evaluators import CodeEvaluationSnapshot, ExperimentEvaluatorSnapshot
from .evaluation_queries import SCORABLE_EXPERIMENT_STATES, missing_evaluations
from .experiment_lifecycle import settle_experiment

SNAPSHOT = TypeAdapter(ExperimentEvaluatorSnapshot)


class EvaluationService:
    def __init__(self, database: Database, sandbox: SnekboxClient | None = None):
        self.database = database
        self.sandbox = sandbox

    async def run(self, item: EvaluationWorkItem) -> str | None:
        started = datetime.now(timezone.utc)
        try:
            snapshot = SNAPSHOT.validate_python(item.snapshot)
            if isinstance(snapshot, CodeEvaluationSnapshot):
                result, error = await self._evaluate_code(item, snapshot)
            else:
                inputs = resolve_inputs(snapshot.input_mapping, item.context)
                result, error = await evaluate_builtin(snapshot.evaluator_key, inputs)
        except (ValidationError, ValueError):
            result, error = EvaluationResult(), "评分输入配置无效或路径缺失"
        except TimeoutError:
            result, error = EvaluationResult(), "评分执行超时"
        return await self._save(item, result, error, started)

    async def _evaluate_code(self, item: EvaluationWorkItem,
                             snapshot: CodeEvaluationSnapshot) -> tuple[EvaluationResult, str | None]:
        if self.sandbox is None:
            return EvaluationResult(), "评分沙箱尚未配置"
        context = CodeEvaluatorContext.model_validate(item.context)
        version = CodeEvaluatorVersion
        # 归档只影响新绑定。通过 Run 所有者限制历史版本的读取范围。
        async with self.database.transaction() as db:
            source_code = await db.scalar(
                select(version.source_code)
                .join(CodeEvaluator, version.evaluator_id == CodeEvaluator.id)
                .join(Experiment, Experiment.user_id == CodeEvaluator.user_id)
                .join(Job, Job.experiment_id == Experiment.id)
                .where(Job.id == item.job_pk, version.id == snapshot.code_evaluator_version_id,
                       version.evaluator_id == snapshot.code_evaluator_id)
            )
        if source_code is None:
            return EvaluationResult(), "评分器发布版本不可用"
        try:
            outcome = await self.sandbox.execute(source_code, context)
        except APIError as exc:
            return EvaluationResult(), exc.message
        if outcome.status == "failed":
            return EvaluationResult(), outcome.error.message
        return EvaluationResult(**outcome.result.model_dump()), None

    async def _save(self, item: EvaluationWorkItem, result: EvaluationResult, error: str | None,
                    started: datetime) -> str | None:
        async with self.database.transaction() as db:
            # 与 Task 完成一致，先锁 Job，再锁 Experiment。取消以实验状态为准。
            job = await db.get(Job, item.job_pk, with_for_update=True)
            if job is None or job.status != "succeeded":
                return
            experiment = await db.get(Experiment, job.experiment_id, with_for_update=True)
            if experiment is None or experiment.status not in SCORABLE_EXPERIMENT_STATES:
                return
            table = JobEvaluation.__table__
            insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            await db.execute(insert(table).values(
                experiment_run_id=item.job_pk, name=item.name, annotator_kind="CODE",
                label=result.label, score=result.score, explanation=result.explanation, error=error,
                start_time=started, end_time=datetime.now(timezone.utc),
            ).on_conflict_do_nothing(index_elements=[table.c.experiment_run_id, table.c.name]))
            if experiment.status == "completed":
                # A late Task result can add more than one score without reopening the experiment.
                if await db.scalar(select(missing_evaluations(db).where(Job.experiment_id == experiment.id).exists())):
                    return None
            else:
                await settle_experiment(db, experiment)
            return experiment.status
