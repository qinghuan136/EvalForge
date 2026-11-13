"""创建固定版本的实验及待执行 Job，查询时批量聚合进度。"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import get_args

from sqlalchemy import and_, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from evalforge.db import Database
from evalforge.db.models import (
    DatasetExampleRevision, DatasetVersion, Experiment, ExperimentDatasetExample, Job, JobEvaluation, Runner,
    RunnerAttempt,
)

from ..errors import APIError
from ..runner_connections import RunnerConnections
from ..schemas.experiments import (
    EvaluationProgress, ExperimentCreate, ExperimentDetail, ExperimentDetailProgress, ExperimentPage,
    ExperimentProgress, ExperimentSummary, JobDetail, JobEvaluationDetail, JobPage, JobStatus, JobSummary,
    SavedExecutionConfig,
)
from .auth import locked_user
from .attempts import ACTIVE
from .datasets import dataset_snapshot, require_owned_dataset
from .evaluators import load_evaluator_snapshots
from .experiment_lifecycle import settle_experiment

MAX_EXPERIMENT_JOBS = 10_000
JOB_STATUSES = get_args(JobStatus)
SUMMARY_FIELDS = (
    "id", "dataset_id", "dataset_version_id", "name", "status", "repetitions", "created_at", "updated_at",
)
JOB_SUMMARY_COLUMNS = (
    Job.id, Job.job_id, Job.dataset_example_id.label("example_id"), Job.repetition_number,
    Job.status, Job.duration_ms, Job.http_status,
)


def _summary_fields(experiment: Experiment, progress: ExperimentProgress) -> dict:
    return {**{name: getattr(experiment, name) for name in SUMMARY_FIELDS}, "progress": progress}


def _detail(experiment: Experiment, progress: ExperimentDetailProgress) -> ExperimentDetail:
    config = {name: experiment.execution_config[name] for name in SavedExecutionConfig.__annotations__
              if name in experiment.execution_config}
    config.setdefault("stream_output", False)
    return ExperimentDetail(**_summary_fields(experiment, progress), description=experiment.description,
                            metadata=experiment.metadata_, execution_config=config,
                            evaluators=experiment.execution_config.get("evaluators", []))


def _evaluator_names(config: dict) -> list[str]:
    return [entry["name"] for entry in config.get("evaluators", [])]


async def _owned_execution_config(db: AsyncSession, owner_id: int, experiment_id: int) -> dict:
    config = await db.scalar(select(Experiment.execution_config).where(
        Experiment.id == experiment_id, Experiment.user_id == owner_id,
    ))
    if config is None:
        raise APIError(404, "experiment_not_found", "实验不存在")
    return config


def _job_summary(row, evaluator_count: int, completed: int, errors: int) -> dict:
    return dict(job_id=row.job_id, example_id=row.example_id, repetition_number=row.repetition_number,
                status=row.status, duration_ms=row.duration_ms, http_status=row.http_status,
                evaluation_progress=EvaluationProgress(
                    expected=evaluator_count if row.status == "succeeded" else 0,
                    completed=completed, errors=errors,
                ))


class ExperimentService:
    def __init__(self, database: Database, connections: RunnerConnections, *,
                 on_created: Callable[[str, int], Awaitable[None]] | None = None,
                 on_cancelled: Callable[[str, int, str], Awaitable[None]] | None = None):
        self.database = database
        self.connections = connections
        self.on_created, self.on_cancelled = on_created, on_cancelled

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

            connection = await self.connections.read_current(data.execution_config.runner_id)
            if connection is None:
                raise APIError(409, "runner_offline", "请先连接实验指定的 Runner")
            if data.execution_config.stream_output and not {"http_json", "http_sse"}.issubset(connection.capabilities):
                raise APIError(409, "runner_streaming_unsupported", "当前 Runner 不支持流式执行，请升级后重连")

            max_examples = MAX_EXPERIMENT_JOBS // data.repetitions
            snapshot = (await db.execute(dataset_snapshot(dataset_id, version_id)
                .order_by("dataset_example_id").limit(max_examples + 1))).all()
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
            await db.execute(insert(Job), [
                {"experiment_id": experiment.id, "dataset_example_id": example_id,
                 "repetition_number": number, "status": "queued"}
                for example_id, _ in snapshot for number in range(1, data.repetitions + 1)
            ])
            count = len(snapshot) * data.repetitions
            result = _detail(experiment, ExperimentDetailProgress(
                example_count=len(snapshot), job_count=count, queued=count,
            ))
        if self.on_created is not None:
            await self.on_created(data.execution_config.runner_id, result.experiment_id)
        return result  # 事务成功提交后才交给 HTTP 层。

    @staticmethod
    async def _progress(db: AsyncSession, experiment_ids: list[int], *,
                        evaluator_names: list[str] | None = None) -> dict[int, ExperimentProgress]:
        if not experiment_ids:
            return {}
        snapshots = ExperimentDatasetExample
        example_counts = {experiment_id: count for experiment_id, count in (await db.execute(
            select(snapshots.experiment_id, func.count()).where(snapshots.experiment_id.in_(experiment_ids))
            .group_by(snapshots.experiment_id)
        )).all()}
        statuses = {experiment_id: {} for experiment_id in experiment_ids}
        scores = {}
        jobs = select(Job.experiment_id, Job.status).where(Job.experiment_id.in_(experiment_ids))
        if evaluator_names:
            # Job 成功数和已保存评分在同一条 SQL 中读取；按 Job 去重以免多个评分放大 Job 计数。
            jobs = jobs.outerjoin(JobEvaluation, and_(
                JobEvaluation.experiment_run_id == Job.id, Job.status == "succeeded",
                JobEvaluation.name.in_(evaluator_names),
            )).add_columns(func.count(Job.id.distinct()), func.count(JobEvaluation.id), func.count(JobEvaluation.error))
        else:
            jobs = jobs.add_columns(func.count())
        for experiment_id, status, count, *score_counts in await db.execute(
            jobs.group_by(Job.experiment_id, Job.status)
        ):
            statuses[experiment_id][status] = count
            if score_counts and status == "succeeded":
                scores[experiment_id] = score_counts

        progress = {}
        for experiment_id, counts in statuses.items():
            fields = {status: counts.get(status, 0) for status in JOB_STATUSES}
            fields.update(example_count=example_counts.get(experiment_id, 0), job_count=sum(counts.values()))
            if evaluator_names is None:
                progress[experiment_id] = ExperimentProgress(**fields)
            else:
                completed, errors = scores.get(experiment_id, (0, 0))
                progress[experiment_id] = ExperimentDetailProgress(**fields, evaluations=EvaluationProgress(
                    expected=fields["succeeded"] * len(evaluator_names), completed=completed, errors=errors,
                ))
        return progress

    async def _get_detail(self, db: AsyncSession, experiment: Experiment) -> ExperimentDetail:
        names = _evaluator_names(experiment.execution_config)
        progress = (await self._progress(db, [experiment.id], evaluator_names=names))[experiment.id]
        return _detail(experiment, progress)

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
            return await self._get_detail(db, experiment)

    async def list_jobs(self, owner_id: int, experiment_id: int, page: int, pagesize: int,
                        status: JobStatus | None) -> JobPage:
        async with self.database.transaction() as db:
            names = _evaluator_names(await _owned_execution_config(db, owner_id, experiment_id))
            scope = [Job.experiment_id == experiment_id]
            if status is not None:
                scope.append(Job.status == status)
            total = await db.scalar(select(func.count()).select_from(Job).where(*scope))
            offset = (page - 1) * pagesize
            if offset >= total:
                return JobPage(items=[], page=page, pagesize=pagesize, total=total)
            jobs = (select(*JOB_SUMMARY_COLUMNS).where(*scope).order_by(Job.id)
                    .offset(offset).limit(pagesize).subquery())
            query = select(*jobs.c)
            if names:
                # 先截取当前页，再关联评分；同一条语句内读取 Job 状态和评分计数。
                query = (query.outerjoin(JobEvaluation, and_(
                    JobEvaluation.experiment_run_id == jobs.c.id, jobs.c.status == "succeeded",
                    JobEvaluation.name.in_(names),
                )).add_columns(func.count(JobEvaluation.id).label("completed"),
                               func.count(JobEvaluation.error).label("errors")).group_by(*jobs.c))
            else:
                query = query.add_columns(literal(0).label("completed"), literal(0).label("errors"))
            rows = await db.execute(query.order_by(jobs.c.id))
            items = [JobSummary(**_job_summary(row, len(names), row.completed, row.errors)) for row in rows]
            return JobPage(items=items, page=page, pagesize=pagesize, total=total)

    async def get_job(self, owner_id: int, experiment_id: int, job_id: str) -> JobDetail:
        async with self.database.transaction() as db:
            names = _evaluator_names(await _owned_execution_config(db, owner_id, experiment_id))
            snapshot, revision = ExperimentDatasetExample, DatasetExampleRevision
            row = (await db.execute(select(
                *JOB_SUMMARY_COLUMNS, Job.output, Job.error, Job.start_time, Job.end_time,
                revision.input, revision.output.label("reference"), revision.metadata_.label("metadata"),
            ).select_from(Job).join(snapshot, and_(snapshot.experiment_id == Job.experiment_id,
                                                  snapshot.dataset_example_id == Job.dataset_example_id))
                .join(revision, revision.id == snapshot.dataset_example_revision_id)
                .where(Job.experiment_id == experiment_id, Job.job_id == job_id))).one_or_none()
            if row is None:
                raise APIError(404, "job_not_found", "Job 不存在")

            evaluations = []
            if names and row.status == "succeeded":
                scores = (await db.scalars(select(JobEvaluation).where(
                    JobEvaluation.experiment_run_id == row.id, JobEvaluation.name.in_(names),
                ))).all()
                by_name = {score.name: score for score in scores}
                evaluations = [JobEvaluationDetail.model_validate(by_name[name]) for name in names if name in by_name]
            return JobDetail(
                **_job_summary(row, len(names), len(evaluations), sum(score.error is not None for score in evaluations)),
                input=row.input, reference=row.reference, metadata=row.metadata, output=(row.output or {}).get("task_output"),
                error=row.error, started_at=row.start_time, ended_at=row.end_time, evaluations=evaluations,
            )

    async def cancel(self, owner_id: int, auth_version: int, experiment_id: int) -> ExperimentDetail:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            scope = (Experiment.id == experiment_id, Experiment.user_id == owner_id)
            config = await _owned_execution_config(db, owner_id, experiment_id)
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
                jobs = select(Job.id).where(Job.experiment_id == experiment_id)
                await db.execute(update(Job).where(
                    Job.experiment_id == experiment_id, Job.status == "queued",
                ).values(status="cancelled", output={"task_output": None}, end_time=now))
                await db.execute(update(RunnerAttempt).where(
                    RunnerAttempt.experiment_run_id.in_(jobs), RunnerAttempt.status.in_(ACTIVE),
                    RunnerAttempt.cancel_requested_at.is_(None),
                ).values(cancel_requested_at=now, cancel_reason="user_requested"))
                await settle_experiment(db, experiment)
                await db.flush()
                await db.refresh(experiment, ["updated_at"])
            result = await self._get_detail(db, experiment)
        if cancelling and isinstance(runner_id, str) and self.on_cancelled is not None:
            await self.on_cancelled(runner_id, experiment_id, result.status)
        return result
