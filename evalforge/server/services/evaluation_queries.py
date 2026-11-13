"""Database definition of a missing score, shared by recovery and completion."""

from sqlalchemy import case, column, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db.base import JSON_VALUE
from evalforge.db.models import DatasetExampleRevision, Experiment, ExperimentDatasetExample, Job, JobEvaluation

from ..experiment_runtime import EvaluationWorkItem

SCORABLE_EXPERIMENT_STATES = ("running", "completed")


def missing_evaluations(db: AsyncSession):
    snapshots = Experiment.execution_config["evaluators"]
    if db.get_bind().dialect.name == "postgresql":
        array = case((func.jsonb_typeof(snapshots) == "array", snapshots), else_=func.jsonb_build_array())
        entries = func.jsonb_array_elements(array).table_valued(column("value", JSON_VALUE))
    else:
        array = case((func.json_type(snapshots) == "array", snapshots), else_=func.json_array())
        entries = func.json_each(array).table_valued(column("value", JSON_VALUE))
    evaluator = entries.alias("evaluator_snapshot")
    name = evaluator.c.value["name"].as_string()
    saved = select(JobEvaluation.id).where(
        JobEvaluation.experiment_run_id == Job.id,
        JobEvaluation.name == name,
    ).exists()
    return (select(Job.id.label("job_pk"), name.label("name"), evaluator.c.value.label("snapshot"))
            .select_from(Job).join(Experiment, Experiment.id == Job.experiment_id)
            .join(evaluator, true())
            .where(Job.status == "succeeded", Experiment.status.in_(SCORABLE_EXPERIMENT_STATES),
                   name.is_not(None), ~saved))


async def missing_evaluation_items(db: AsyncSession, *scope) -> list[tuple[int, EvaluationWorkItem]]:
    snapshot, revision = ExperimentDatasetExample, DatasetExampleRevision
    query = missing_evaluations(db)
    query = (query.join(snapshot, (snapshot.experiment_id == Job.experiment_id)
                       & (snapshot.dataset_example_id == Job.dataset_example_id))
            .join(revision, revision.id == snapshot.dataset_example_revision_id)
            .add_columns(Job.experiment_id, Job.output, revision.input,
                         revision.output.label("reference"), revision.metadata_)
             .where(*scope).order_by(Job.id, query.selected_columns.name))
    return [(row.experiment_id, evaluation_work_item(row)) for row in await db.execute(query)]


def evaluation_work_item(row) -> EvaluationWorkItem:
    return EvaluationWorkItem(row.job_pk, row.name, row.snapshot, {
        "input": row.input, "reference": row.reference,
        "output": (row.output or {}).get("task_output"), "metadata": row.metadata_,
    })
