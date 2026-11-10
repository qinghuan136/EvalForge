"""Shared completion rules for Task and Eval transactions."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db.models import Experiment, ExperimentRun

from .evaluation_queries import missing_evaluations

TERMINAL = {"succeeded", "failed", "cancelled"}
SETTLED_JOBS = TERMINAL | {"unknown"}


async def settle_experiment(db: AsyncSession, experiment: Experiment) -> None:
    """Check both stages while the caller holds the experiment row lock."""
    if experiment.status not in {"running", "cancelling"}:
        return
    unfinished_tasks = select(ExperimentRun.id).where(
        ExperimentRun.experiment_id == experiment.id, ExperimentRun.status.not_in(SETTLED_JOBS),
    ).exists()
    if await db.scalar(select(unfinished_tasks)):
        return
    if experiment.status == "cancelling":
        experiment.status = "cancelled"
        return
    if experiment.execution_config.get("evaluators"):
        pending = missing_evaluations(db).where(ExperimentRun.experiment_id == experiment.id).exists()
        if await db.scalar(select(pending)):
            return
    experiment.status = "completed"


async def finish_experiment(db: AsyncSession, experiment_id: int, runner_id: str) -> None:
    # Re-read after acquiring the lock so concurrent Task/Eval commits are visible.
    experiment = await db.get(Experiment, experiment_id, with_for_update=True, populate_existing=True)
    if experiment is not None and experiment.execution_config.get("runner_id") == runner_id:
        await settle_experiment(db, experiment)
