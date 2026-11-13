"""Load a Runner's persisted experiment work outside the Scheduler loop."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from evalforge.db import Database
from evalforge.db.models import DatasetExampleRevision, Experiment, ExperimentDatasetExample, Job

from ..experiment_runtime import ExperimentRuntime, JobWorkItem
from ..runner_auth import RunnerIdentity
from .dispatch import ActiveAssignment, active_assignment
from .evaluation_queries import missing_evaluation_items
from .experiment_lifecycle import ACTIVE_EXPERIMENTS


@dataclass(slots=True)
class RuntimeSnapshot:
    experiments: dict[int, ExperimentRuntime]
    active: ActiveAssignment | None


class RuntimeLoader:
    def __init__(self, database: Database):
        self.database = database

    async def load(self, identity: RunnerIdentity, *, experiment_id: int | None = None) -> RuntimeSnapshot:
        scope = (
            Experiment.user_id == identity.owner_user_id,
            Experiment.execution_config["runner_id"].as_string() == identity.runner_id,
        )
        if experiment_id is not None:
            scope += (Experiment.id == experiment_id,)
        async with self.database.transaction() as db:
            experiments = (await db.execute(
                select(Experiment.id, Experiment.execution_config, Experiment.status)
                .where(*scope, Experiment.status.in_(ACTIVE_EXPERIMENTS)).order_by(Experiment.id)
            )).all()
            runtimes = {row.id: ExperimentRuntime.from_saved(row.id, row.execution_config, row.status)
                        for row in experiments}
            if runtimes:
                await self._load_tasks(db, scope, runtimes)
            # Completed experiments can still acquire a late successful result from an unknown Attempt.
            evaluations = await missing_evaluation_items(db, *scope)
            extra_ids = {pk for pk, _ in evaluations if pk not in runtimes}
            if extra_ids:
                for row in await db.execute(select(Experiment.id, Experiment.execution_config, Experiment.status)
                                            .where(*scope, Experiment.id.in_(extra_ids), Experiment.status == "completed")):
                    runtimes[row.id] = ExperimentRuntime.from_saved(row.id, row.execution_config, row.status)
            for pk, item in evaluations:
                if pk in runtimes:
                    runtimes[pk].evals[item.key] = item
            active = await active_assignment(db, identity) if experiment_id is None else None
        return RuntimeSnapshot(runtimes, active)

    @staticmethod
    async def _load_tasks(db: AsyncSession, scope: tuple[ColumnElement[bool], ...],
                          runtimes: dict[int, ExperimentRuntime]) -> None:
        snapshot = ExperimentDatasetExample
        queued = (await db.execute(
            select(Job.experiment_id, Job.id, Job.job_id, Job.dataset_example_id, DatasetExampleRevision.input)
            .join(Experiment, Experiment.id == Job.experiment_id)
            .join(snapshot, (snapshot.experiment_id == Job.experiment_id)
                  & (snapshot.dataset_example_id == Job.dataset_example_id))
            .join(DatasetExampleRevision, DatasetExampleRevision.id == snapshot.dataset_example_revision_id)
            .where(*scope, Job.status == "queued", Experiment.status.in_(("pending", "running")))
            .order_by(Job.created_at, Job.id)
        )).all()
        for row in queued:
            if (runtime := runtimes.get(row.experiment_id)) is not None:
                runtime.tasks[row.id] = JobWorkItem(row.id, row.job_id, row.dataset_example_id, row.input)
