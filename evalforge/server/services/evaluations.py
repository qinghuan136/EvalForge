"""Discover and execute missing scores against the experiment's saved snapshots."""

from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from evalforge.db import Database
from evalforge.db.models import Experiment, Job, JobEvaluation
from evalforge.models import EvaluationResult

from ..evaluators.execution import evaluate_builtin
from ..evaluators.input_mapping import resolve_inputs
from ..experiment_runtime import EvaluationWorkItem
from ..schemas.evaluators import EvaluatorSnapshot
from .evaluation_queries import SCORABLE_EXPERIMENT_STATES, missing_evaluations
from .experiment_lifecycle import settle_experiment


class EvaluationService:
    def __init__(self, database: Database):
        self.database = database

    async def run(self, item: EvaluationWorkItem) -> str | None:
        started = datetime.now(timezone.utc)
        result, error = EvaluationResult(), None
        try:
            snapshot = EvaluatorSnapshot.model_validate(item.snapshot)
            inputs = resolve_inputs(snapshot.input_mapping, item.context)
        except (ValidationError, ValueError):
            error = "评分输入配置无效或路径缺失"
        else:
            try:
                result, error = await evaluate_builtin(snapshot.evaluator_key, inputs)
            except TimeoutError:
                error = "评分执行超时"
        return await self._save(item, result, error, started)

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
