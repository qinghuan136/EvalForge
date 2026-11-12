"""执行链测试数据：调度器尚未实现，直接预置已分配的尝试。"""

from uuid import uuid4

from evalforge.db.models import Dataset, DatasetExample, DatasetVersion, Experiment, Job, RunnerAttempt


async def seed_attempt(database, owner_id, runner_id):
    async with database.transaction() as db:
        dataset = Dataset(name=str(uuid4()), user_id=owner_id)
        db.add(dataset)
        await db.flush()
        version = DatasetVersion(dataset_id=dataset.id)
        example = DatasetExample(dataset_id=dataset.id)
        db.add_all([version, example])
        await db.flush()
        experiment = Experiment(dataset_id=dataset.id, dataset_version_id=version.id, name="test",
                                repetitions=1, user_id=owner_id, status="running")
        db.add(experiment)
        await db.flush()
        job = Job(experiment_id=experiment.id, dataset_example_id=example.id,
                            repetition_number=1, status="assigned")
        db.add(job)
        await db.flush()
        attempt = RunnerAttempt(experiment_run_id=job.id, runner_id=runner_id, dispatch_json={})
        db.add(attempt)
        await db.flush()
        return job.job_id, attempt.attempt_id


def result_params(key, **changes):
    result = dict(job_id=key[0], attempt_id=key[1], status="succeeded", output={"reply": "你好"},
                  duration_ms=10, http_status=200, error=None)
    result.update(changes)
    return result
