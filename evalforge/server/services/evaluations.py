"""Discover and execute missing scores against the experiment's saved snapshots."""

from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import JsonValue, ValidationError
from sqlalchemy import and_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from evalforge.db import Database
from evalforge.db.models import (
    DatasetExampleRevision, Experiment, ExperimentDatasetExample, ExperimentRun, ExperimentRunAnnotation,
)
from evalforge.models import EvaluationResult

from ..evaluators.execution import evaluate_builtin
from ..evaluators.input_mapping import resolve_inputs
from ..schemas.evaluators import EvaluatorSnapshot
from .evaluation_queries import SCORABLE_EXPERIMENT_STATES, missing_evaluations
from .experiment_lifecycle import settle_experiment

@dataclass(frozen=True, slots=True)
class PendingEvaluation:
    run_id: int
    name: str
    snapshot: dict[str, JsonValue]
    context: dict[str, JsonValue]

    @property
    def key(self) -> tuple[int, str]:
        return self.run_id, self.name


class EvaluationService:
    def __init__(self, database: Database):
        self.database = database

    async def find_missing(self, run_ids: list[int] | None = None, *,
                           after: tuple[int, str] | None = None, limit: int | None = None) -> list[PendingEvaluation]:
        async with self.database.transaction() as db:
            jobs, examples, revision = ExperimentRun, ExperimentDatasetExample, DatasetExampleRevision
            query = missing_evaluations(db)
            name = query.selected_columns.name
            query = (query.join(examples, and_(examples.experiment_id == jobs.experiment_id,
                                               examples.dataset_example_id == jobs.dataset_example_id))
                     .join(revision, revision.id == examples.dataset_example_revision_id)
                     .add_columns(jobs.output, revision.input, revision.output.label("reference"), revision.metadata_)
                     .order_by(jobs.id, name))
            if run_ids is not None:
                query = query.where(jobs.id.in_(run_ids))
            if after is not None:
                query = query.where(tuple_(jobs.id, name) > after)
            if limit is not None:
                query = query.limit(limit)
            rows = (await db.execute(query)).all()
        return [PendingEvaluation(row.run_id, row.name, row.snapshot, {
            "input": row.input, "reference": row.reference,
            "output": (row.output or {}).get("task_output"), "metadata": row.metadata_,
        }) for row in rows]

    async def run(self, item: PendingEvaluation) -> None:
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
        await self._save(item, result, error, started)

    async def _save(self, item: PendingEvaluation, result: EvaluationResult, error: str | None,
                    started: datetime) -> None:
        async with self.database.transaction() as db:
            # 与 Task 完成一致，先锁 Job，再锁 Experiment。取消以实验状态为准。
            job = await db.get(ExperimentRun, item.run_id, with_for_update=True)
            if job is None or job.status != "succeeded":
                return
            experiment = await db.get(Experiment, job.experiment_id, with_for_update=True)
            if experiment is None or experiment.status not in SCORABLE_EXPERIMENT_STATES:
                return
            table = ExperimentRunAnnotation.__table__
            insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            await db.execute(insert(table).values(
                experiment_run_id=item.run_id, name=item.name, annotator_kind="CODE",
                label=result.label, score=result.score, explanation=result.explanation, error=error,
                start_time=started, end_time=datetime.now(timezone.utc),
            ).on_conflict_do_nothing(index_elements=[table.c.experiment_run_id, table.c.name]))
            await settle_experiment(db, experiment)
