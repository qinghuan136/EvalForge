"""Resolve page references and render the model-facing message context."""

import json
from dataclasses import dataclass, replace
from collections.abc import Awaitable, Callable
from typing import assert_never

from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db.models import DatasetVersion, Experiment, Job, Runner

from ..errors import APIError
from ..schemas.runners import RunnerCreateResponse
from ..schemas.page_context import DatasetPageContext, ExperimentPageContext, JobPageContext, PageContext, RunnerPageContext
from ..services.datasets import DatasetService, require_owned_dataset
from ..services.experiments import ExperimentService
from ..services.runners import RunnerService
from ..services.traces import TraceQueryService


@dataclass
class AssistantDeps:
    owner_id: int
    experiments: ExperimentService
    runners: RunnerService
    datasets: DatasetService
    traces: TraceQueryService
    auth_version: int
    check_execution: Callable[[], Awaitable[None]]
    deliver_credential: Callable[[str, RunnerCreateResponse], None]
    source_experiment_id: int | None = None


async def require_accessible_page_context(db: AsyncSession, user_id: int, context: PageContext | None) -> None:
    """Check ownership and parent references without loading business data."""
    if context is None:
        return
    if isinstance(context, ExperimentPageContext):
        query = select(Experiment.id).where(Experiment.id == context.id, Experiment.user_id == user_id)
        code, message = "experiment_not_found", "实验不存在"
    elif isinstance(context, JobPageContext):
        query = (select(Job.id).join(Experiment, Experiment.id == Job.experiment_id)
                 .where(Job.job_id == context.job_id, Experiment.id == context.experiment_id,
                        Experiment.user_id == user_id))
        code, message = "job_not_found", "Job 不存在"
    elif isinstance(context, DatasetPageContext):
        await require_owned_dataset(db, user_id, context.id)
        if context.version_id is None:
            return
        query = select(DatasetVersion.id).where(
            DatasetVersion.id == context.version_id, DatasetVersion.dataset_id == context.id,
        )
        code, message = "dataset_version_not_found", "数据集版本不存在"
    elif isinstance(context, RunnerPageContext):
        query = select(Runner.runner_id).where(
            Runner.runner_id == context.id, Runner.owner_user_id == user_id, Runner.deleted_at.is_(None),
        )
        code, message = "runner_not_found", "Runner 不存在"
    else:
        assert_never(context)
    if await db.scalar(query) is None:
        raise APIError(404, code, message)


def prompt_with_page_context(text: str, context: dict | None) -> str:
    if context is None:
        return text
    # Keep strings inside the JSON block even if a resource ID contains '<'.
    payload = json.dumps(context, ensure_ascii=False, separators=(",", ":")).replace("<", r"\u003c")
    return f"<page_context>{payload}</page_context>\n\n{text}"


def with_user_text(message: ModelRequest, text: str) -> ModelRequest:
    return replace(message, parts=[
        replace(part, content=text) if isinstance(part, UserPromptPart) else part
        for part in message.parts
    ])
