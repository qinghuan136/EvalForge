from pydantic_ai import RunContext

from ..schemas.page_context import ExperimentPageContext
from .context import AssistantDeps


async def get_experiment_progress(ctx: RunContext[AssistantDeps]) -> dict:
    """读取当前页面实验的状态、Job 汇总和评分进度。"""
    deps = ctx.deps
    assert isinstance(deps.page_context, ExperimentPageContext)
    result = await deps.experiments.get(deps.owner_id, deps.page_context.id)
    deps.progress_read = True
    return {
        "experiment_id": result.experiment_id,
        "status": result.status,
        "progress": result.progress.model_dump(mode="json"),
    }
