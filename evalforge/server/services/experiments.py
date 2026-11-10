"""创建固定版本的实验及待执行 Job，查询时批量聚合进度。"""

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from evalforge.db import Database
from evalforge.db.models import (
    DatasetExample, DatasetExampleRevision, DatasetVersion, Experiment,
    ExperimentDatasetExample, ExperimentRun, Runner, RunnerAttempt,
)

from ..errors import APIError
from ..schemas.experiments import (
    ExperimentCreate, ExperimentDetail, ExperimentPage, ExperimentProgress, ExperimentSummary,
    SavedExecutionConfig,
)
from .auth import locked_user
from .attempts import ACTIVE
from .datasets import require_owned_dataset
from .evaluators import load_evaluator_snapshots
from .experiment_lifecycle import settle_experiment

MAX_EXPERIMENT_JOBS = 10_000
JOB_STATUSES = ("queued", "assigned", "running", "succeeded", "failed", "cancelled", "unknown")
SUMMARY_FIELDS = (
    "id", "dataset_id", "dataset_version_id", "name", "status", "repetitions", "created_at", "updated_at",
)


def _summary_fields(experiment: Experiment, progress: ExperimentProgress) -> dict:
    return {**{name: getattr(experiment, name) for name in SUMMARY_FIELDS}, "progress": progress}


def _detail(experiment: Experiment, progress: ExperimentProgress) -> ExperimentDetail:
    config = {name: experiment.execution_config[name] for name in SavedExecutionConfig.__annotations__
              if name in experiment.execution_config}
    return ExperimentDetail(**_summary_fields(experiment, progress), description=experiment.description,
                            metadata=experiment.metadata_, execution_config=config,
                            evaluators=experiment.execution_config.get("evaluators", []))


def _snapshot_query(dataset_id: int, version_id: int, limit: int):
    revision = DatasetExampleRevision
    ranked = (
        select(revision.dataset_example_id, revision.id.label("revision_id"), revision.revision_kind,
               func.row_number().over(partition_by=revision.dataset_example_id,
                                      order_by=revision.dataset_version_id.desc()).label("rank"))
        .join(DatasetExample, DatasetExample.id == revision.dataset_example_id)
        .join(DatasetVersion, DatasetVersion.id == revision.dataset_version_id)
        .where(DatasetExample.dataset_id == dataset_id, DatasetVersion.dataset_id == dataset_id,
               revision.dataset_version_id <= version_id)
        .subquery()
    )
    # 先选最新修订再排除 DELETE，否则会复活已删除的样本。
    return (select(ranked.c.dataset_example_id, ranked.c.revision_id)
            .where(ranked.c.rank == 1, ranked.c.revision_kind != "DELETE")
            .order_by(ranked.c.dataset_example_id).limit(limit))


class ExperimentService:
    def __init__(self, database: Database, on_work_available: Callable[[], None] | None = None):
        self.database = database
        self.on_work_available = on_work_available

    async def create(self, owner_id: int, auth_version: int, dataset_id: int,
                     data: ExperimentCreate) -> ExperimentDetail:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            await require_owned_dataset(db, owner_id, dataset_id, lock=True)
            versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset_id)
            if data.dataset_version_id is not None:
                versions = versions.where(DatasetVersion.id == data.dataset_version_id)
            version_id = await db.scalar(versions.order_by(DatasetVersion.id.desc()).limit(1).with_for_update(read=True))
            if version_id is None:
                raise APIError(404, "dataset_version_not_found", "数据集版本不存在")

            enabled = await db.scalar(select(Runner.enabled).where(
                Runner.runner_id == data.execution_config.runner_id, Runner.owner_user_id == owner_id,
                Runner.deleted_at.is_(None),
            ).with_for_update(read=True))
            if enabled is None:
                raise APIError(404, "runner_not_found", "Runner 不存在")
            if not enabled:
                raise APIError(409, "runner_disabled", "Runner 已禁用")

            max_examples = MAX_EXPERIMENT_JOBS // data.repetitions
            snapshot = (await db.execute(_snapshot_query(dataset_id, version_id, max_examples + 1))).all()
            if not snapshot:
                raise APIError(422, "empty_experiment_dataset", "数据集版本没有有效样本")
            if len(snapshot) > max_examples:
                raise APIError(422, "experiment_job_limit_exceeded", "实验 Job 数量超过单次创建上限")

            evaluators = await load_evaluator_snapshots(db, dataset_id, data.dataset_evaluator_ids)
            execution_config = data.execution_config.model_dump()
            if evaluators:
                execution_config["evaluators"] = [evaluator.model_dump(mode="json") for evaluator in evaluators]
            experiment = Experiment(user_id=owner_id, dataset_id=dataset_id, dataset_version_id=version_id,
                                    name=data.name, description=data.description, repetitions=data.repetitions,
                                    status="pending", metadata_=data.metadata,
                                    execution_config=execution_config)
            db.add(experiment)
            await db.flush()
            await db.execute(insert(ExperimentDatasetExample), [
                {"experiment_id": experiment.id, "dataset_example_id": example_id,
                 "dataset_example_revision_id": revision_id}
                for example_id, revision_id in snapshot
            ])
            await db.execute(insert(ExperimentRun), [
                {"experiment_id": experiment.id, "dataset_example_id": example_id,
                 "repetition_number": number, "status": "queued"}
                for example_id, _ in snapshot for number in range(1, data.repetitions + 1)
            ])
            count = len(snapshot) * data.repetitions
            result = _detail(experiment, ExperimentProgress(example_count=len(snapshot), job_count=count, queued=count))
        if self.on_work_available is not None:
            self.on_work_available()
        return result  # 事务成功提交后才交给 HTTP 层。

    @staticmethod
    async def _progress(db: AsyncSession, experiment_ids: list[int]) -> dict[int, ExperimentProgress]:
        if not experiment_ids:
            return {}
        snapshots = ExperimentDatasetExample
        example_counts = {experiment_id: count for experiment_id, count in (await db.execute(
            select(snapshots.experiment_id, func.count()).where(snapshots.experiment_id.in_(experiment_ids))
            .group_by(snapshots.experiment_id)
        )).all()}
        runs = ExperimentRun
        statuses = {experiment_id: {} for experiment_id in experiment_ids}
        for experiment_id, status, count in (await db.execute(
            select(runs.experiment_id, runs.status, func.count()).where(runs.experiment_id.in_(experiment_ids))
            .group_by(runs.experiment_id, runs.status)
        )).all():
            statuses[experiment_id][status] = count
        return {experiment_id: ExperimentProgress(
            example_count=example_counts.get(experiment_id, 0), job_count=sum(counts.values()),
            **{status: counts.get(status, 0) for status in JOB_STATUSES},
        ) for experiment_id, counts in statuses.items()}

    async def list(self, owner_id: int, dataset_id: int, page: int, pagesize: int) -> ExperimentPage:
        async with self.database.transaction() as db:
            await require_owned_dataset(db, owner_id, dataset_id)
            scope = (Experiment.dataset_id == dataset_id, Experiment.user_id == owner_id)
            total = await db.scalar(select(func.count()).select_from(Experiment).where(*scope))
            experiments = (await db.scalars(select(Experiment).where(*scope)
                .options(load_only(*(getattr(Experiment, name) for name in SUMMARY_FIELDS)))
                .order_by(Experiment.created_at.desc(), Experiment.id.desc())
                .offset((page - 1) * pagesize).limit(pagesize))).all()
            progress = await self._progress(db, [row.id for row in experiments])
            return ExperimentPage(items=[ExperimentSummary(**_summary_fields(row, progress[row.id]))
                                         for row in experiments], page=page, pagesize=pagesize, total=total)

    async def get(self, owner_id: int, experiment_id: int) -> ExperimentDetail:
        async with self.database.transaction() as db:
            experiment = await db.scalar(select(Experiment).where(
                Experiment.id == experiment_id, Experiment.user_id == owner_id,
            ))
            if experiment is None:
                raise APIError(404, "experiment_not_found", "实验不存在")
            return _detail(experiment, (await self._progress(db, [experiment_id]))[experiment_id])

    async def cancel(self, owner_id: int, auth_version: int, experiment_id: int) -> ExperimentDetail:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            scope = (Experiment.id == experiment_id, Experiment.user_id == owner_id)
            config = await db.scalar(select(Experiment.execution_config).where(*scope))
            if config is None:
                raise APIError(404, "experiment_not_found", "实验不存在")
            # 先锁 Runner，再读取并锁定实验。离线或停用的 Runner 也可以取消。
            runner_id = config.get("runner_id")
            if isinstance(runner_id, str):
                await db.scalar(select(Runner.runner_id).where(
                    Runner.runner_id == runner_id, Runner.owner_user_id == owner_id,
                ).with_for_update())
            experiment = await db.scalar(select(Experiment).where(*scope).with_for_update())
            if experiment is None:
                raise APIError(404, "experiment_not_found", "实验不存在")
            cancelling = experiment.status in {"pending", "running", "cancelling"}
            if cancelling:
                now = datetime.now(timezone.utc)
                experiment.status = "cancelling"
                jobs = select(ExperimentRun.id).where(ExperimentRun.experiment_id == experiment_id)
                await db.execute(update(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment_id, ExperimentRun.status == "queued",
                ).values(status="cancelled", output={"task_output": None}, end_time=now))
                await db.execute(update(RunnerAttempt).where(
                    RunnerAttempt.experiment_run_id.in_(jobs), RunnerAttempt.status.in_(ACTIVE),
                    RunnerAttempt.cancel_requested_at.is_(None),
                ).values(cancel_requested_at=now, cancel_reason="user_requested"))
                await settle_experiment(db, experiment)
                await db.flush()
                await db.refresh(experiment, ["updated_at"])
            progress = (await self._progress(db, [experiment_id]))[experiment_id]
            result = _detail(experiment, progress)
        if cancelling and self.on_work_available is not None:
            self.on_work_available()
        return result
