"""Tools backed by the current user's business services."""

from collections.abc import Awaitable
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, ValidationError
from pydantic_ai import RunContext
from starlette.concurrency import run_in_threadpool

from ..dataset_import import parse_dataset_upload
from ..errors import APIError
from ..schemas.common import MAX_DB_ID, PositiveDbId, StoredText
from ..schemas.datasets import MAX_DATASET_UPLOAD_BYTES, DatasetImportOptions
from ..schemas.evaluators import EvaluatorConfig
from ..schemas.experiments import ExecutionConfig, ExperimentCreate, JobStatus
from ..schemas.runners import RunnerName
from ..schemas.assistant import AssistantDisplayCard, AssistantDisplayField
from .context import AssistantDeps

Page = Annotated[int, Field(ge=1, le=MAX_DB_ID)]
PageSize = Annotated[int, Field(ge=1, le=100)]
ResourceId = Annotated[StoredText, Field(min_length=1, max_length=128, pattern=r"\S")]
TraceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
SpanId = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
ApprovalDescription = Annotated[StoredText, Field(max_length=500)]


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


async def _read(result: Awaitable[BaseModel]) -> dict:
    try:
        return (await result).model_dump(mode="json")
    except APIError as exc:
        return _error(exc.code, exc.message)


async def RUNNER_create_runner(ctx: RunContext[AssistantDeps], name: str,
                               approval_description: ApprovalDescription | None = None) -> dict:
    """Create a Runner by name after user approval. Credentials are delivered separately."""
    try:
        data = RunnerName(name=name)
    except ValidationError:
        return _error("invalid_request", "Runner 名称须为 1–100 个字符")
    await ctx.deps.check_execution()
    try:
        result = await ctx.deps.runners.create(ctx.deps.owner_id, ctx.deps.auth_version, data.name)
    except APIError as exc:
        return _error(exc.code, exc.message)
    await ctx.deps.display(AssistantDisplayCard(
        id=f"runner-credential:{ctx.tool_call_id}", tool_call_id=ctx.tool_call_id,
        title="Runner 已创建", description="请保存连接凭证，用于配置本地 Runner。刷新页面后无法再次查看。",
        fields=[
            AssistantDisplayField(label="名称", value=result.runner.name),
            AssistantDisplayField(label="Runner ID", value=result.runner.runner_id),
            AssistantDisplayField(label="连接凭证", value=result.credential.token, sensitive=True),
            AssistantDisplayField(label="凭证有效期", value=result.credential.expires_at.isoformat()),
            AssistantDisplayField(label="连接地址", value=result.connection.websocket_url),
        ],
    ))
    return result.runner.model_dump(mode="json")


async def RUNNER_delete_runner(ctx: RunContext[AssistantDeps], runner_id: ResourceId,
                               approval_description: ApprovalDescription | None = None) -> dict:
    """Delete the specified Runner and revoke its credentials after user approval."""
    await ctx.deps.check_execution()
    try:
        await ctx.deps.runners.delete(ctx.deps.owner_id, ctx.deps.auth_version, runner_id)
    except APIError as exc:
        return _error(exc.code, exc.message)
    return {"runner_id": runner_id, "deleted": True}


async def RUNNER_list_runners(ctx: RunContext[AssistantDeps], page: Page = 1, pagesize: PageSize = 20) -> dict:
    """List the user's Runners and their connection state and capabilities."""
    return await _read(ctx.deps.runners.list(ctx.deps.owner_id, page, pagesize))


async def RUNNER_get_runner_detail(ctx: RunContext[AssistantDeps], runner_id: ResourceId) -> dict:
    """Read a Runner's details and current connection state by Runner ID."""
    return await _read(ctx.deps.runners.get(ctx.deps.owner_id, runner_id))


async def DATASET_import_dataset(
    ctx: RunContext[AssistantDeps], name: str, format: Literal["csv", "jsonl"], content: StoredText,
    description: str | None = None, input_keys: list[str] | None = None,
    output_keys: list[str] | None = None, metadata_keys: list[str] | None = None,
    approval_description: ApprovalDescription | None = None,
) -> dict:
    """Import a dataset after approval. CSV requires input columns; JSONL uses input, output and metadata objects."""
    try:
        options = DatasetImportOptions(name=name, format=format, description=description,
                                       input_keys=input_keys or [], output_keys=output_keys or [],
                                       metadata_keys=metadata_keys or [])
        payload = content.encode("utf-8")
        if len(payload) > MAX_DATASET_UPLOAD_BYTES:
            return _error("dataset_file_too_large", "文件不能超过 5 MiB")
        drafts = await run_in_threadpool(parse_dataset_upload, payload, options)
    except ValidationError:
        return _error("invalid_request", "数据集导入参数不合法")
    except APIError as exc:
        return _error(exc.code, exc.message)
    await ctx.deps.check_execution()
    return await _read(ctx.deps.datasets.import_dataset(ctx.deps.owner_id, ctx.deps.auth_version, options, drafts))


async def DATASET_list_datasets(ctx: RunContext[AssistantDeps], page: Page = 1, pagesize: PageSize = 20) -> dict:
    """List the current user's datasets and their latest versions."""
    return await _read(ctx.deps.datasets.list_datasets(ctx.deps.owner_id, page, pagesize))


async def DATASET_get_dataset_detail(ctx: RunContext[AssistantDeps], dataset_id: PositiveDbId) -> dict:
    """Read one dataset, including metadata and its example count."""
    return await _read(ctx.deps.datasets.get(ctx.deps.owner_id, dataset_id))


async def DATASET_list_examples(ctx: RunContext[AssistantDeps], dataset_id: PositiveDbId,
                                version_id: PositiveDbId | None = None, page: Page = 1, pagesize: PageSize = 20) -> dict:
    """List examples from the specified dataset version, or the latest version when omitted."""
    return await _read(ctx.deps.datasets.list_examples(ctx.deps.owner_id, dataset_id, version_id, page, pagesize))


async def EXPERIMENT_create_experiment(
    ctx: RunContext[AssistantDeps], dataset_id: PositiveDbId, name: str, execution_config: ExecutionConfig,
    dataset_version_id: PositiveDbId, description: str | None = None, repetitions: int = 1,
    metadata: dict[str, JsonValue] | None = None, dataset_evaluator_ids: list[PositiveDbId] | None = None,
    evaluators: list[EvaluatorConfig] | None = None,
    approval_description: ApprovalDescription | None = None,
) -> dict:
    """Create an experiment after approval. First query the dataset and supply its explicit version ID."""
    try:
        data = ExperimentCreate.model_validate({
            "name": name, "execution_config": execution_config, "description": description,
            "repetitions": repetitions, "metadata": metadata or {},
            "dataset_evaluator_ids": dataset_evaluator_ids or [], "evaluators": evaluators or [],
            "dataset_version_id": dataset_version_id,
        })
        examples = await ctx.deps.datasets.list_examples(ctx.deps.owner_id, dataset_id, dataset_version_id, 1, 1)
    except ValidationError:
        return _error("invalid_request", "实验创建参数不合法")
    except APIError as exc:
        return _error(exc.code, exc.message)
    if examples.dataset_version_id is None or not examples.total:
        return _error("empty_experiment_dataset", "数据集版本没有有效样本")
    await ctx.deps.check_execution()
    return await _read(ctx.deps.experiments.create(ctx.deps.owner_id, ctx.deps.auth_version, dataset_id, data))


async def EXPERIMENT_list_experiments(ctx: RunContext[AssistantDeps], dataset_id: PositiveDbId,
                                      page: Page = 1, pagesize: PageSize = 20) -> dict:
    """List experiments for the selected dataset, with their status and Job counts."""
    return await _read(ctx.deps.experiments.list(ctx.deps.owner_id, dataset_id, page, pagesize))


async def EXPERIMENT_get_experiment_detail(ctx: RunContext[AssistantDeps], experiment_id: PositiveDbId) -> dict:
    """Read the experiment's configuration, execution status, Job counts and evaluation progress."""
    result = await _read(ctx.deps.experiments.get(ctx.deps.owner_id, experiment_id))
    if "error" not in result:
        ctx.deps.source_experiment_id = experiment_id
    return result


async def EXPERIMENT_cancel_experiment(ctx: RunContext[AssistantDeps], experiment_id: PositiveDbId,
                                       approval_description: ApprovalDescription | None = None) -> dict:
    """Cancel the entire experiment after user approval, keeping its execution history."""
    await ctx.deps.check_execution()
    return await _read(ctx.deps.experiments.cancel(ctx.deps.owner_id, ctx.deps.auth_version, experiment_id))


async def JOB_list_jobs(ctx: RunContext[AssistantDeps], experiment_id: PositiveDbId, status: JobStatus | None = None,
                        page: Page = 1, pagesize: PageSize = 20) -> dict:
    """List Jobs in an experiment. Optionally filter by execution status."""
    return await _read(ctx.deps.experiments.list_jobs(ctx.deps.owner_id, experiment_id, page, pagesize, status))


async def JOB_get_job_detail(ctx: RunContext[AssistantDeps], experiment_id: PositiveDbId, job_id: ResourceId) -> dict:
    """Read one Job's input, reference, output, errors, evaluation records and associated Trace ID."""
    return await _read(ctx.deps.experiments.get_job(ctx.deps.owner_id, experiment_id, job_id))


async def TRACE_list_traces(ctx: RunContext[AssistantDeps], page: Page = 1, pagesize: PageSize = 20,
                            trace_id: TraceId | None = None) -> dict:
    """List the user's traces, optionally filtered by an exact Trace ID."""
    return await _read(ctx.deps.traces.list_traces(ctx.deps.owner_id, page, pagesize, trace_id))


async def TRACE_get_trace_detail(ctx: RunContext[AssistantDeps], trace_id: TraceId, after_span_id: SpanId | None = None,
                                 limit: Annotated[int, Field(ge=1, le=500)] = 250) -> dict:
    """Read a Trace summary and one page of Spans. Use next_span_id as the next after_span_id."""
    return await _read(ctx.deps.traces.get_trace(ctx.deps.owner_id, trace_id, after_span_id, limit))


async def TRACE_get_span_detail(ctx: RunContext[AssistantDeps], trace_id: TraceId, span_id: SpanId) -> dict:
    """Read the selected Span's timing, status, attributes, events, links and resource fields."""
    return await _read(ctx.deps.traces.get_span(ctx.deps.owner_id, trace_id, span_id))


WRITE_TOOL_NAMES = frozenset({
    "RUNNER_create_runner", "RUNNER_delete_runner", "DATASET_import_dataset",
    "EXPERIMENT_create_experiment", "EXPERIMENT_cancel_experiment",
})

ASSISTANT_TOOLS = (
    RUNNER_create_runner, RUNNER_delete_runner, RUNNER_list_runners, RUNNER_get_runner_detail,
    DATASET_import_dataset, DATASET_list_datasets, DATASET_get_dataset_detail, DATASET_list_examples,
    EXPERIMENT_create_experiment, EXPERIMENT_list_experiments, EXPERIMENT_get_experiment_detail,
    EXPERIMENT_cancel_experiment, JOB_list_jobs, JOB_get_job_detail,
    TRACE_list_traces, TRACE_get_trace_detail, TRACE_get_span_detail,
)
