"""实验 API 契约：版本快照、创建事务、归属、分页及 Job 进度。"""

from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError

from api_support import APITestCase
from evalforge.db.models import (
    Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion, Experiment,
    ExperimentDatasetExample, ExperimentRun, Runner, RunnerAttempt,
)


class ExperimentAPITests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()
        self.owner_id = (await self.user()).id
        async with self.database.transaction() as db:
            dataset = Dataset(name="订单测试集", user_id=self.owner_id)
            runner = Runner(name="离线开发机", owner_user_id=self.owner_id)
            db.add_all([dataset, runner])
            await db.flush()
            self.dataset_id, self.runner_id = dataset.id, runner.runner_id
            self.v1 = await self.version(db, dataset.id)
            examples = [DatasetExample(dataset_id=dataset.id) for _ in range(3)]
            db.add_all(examples)
            await db.flush()
            self.example_ids = [row.id for row in examples]
            self.original_revisions = [await self.revise(db, row.id, self.v1, "CREATE") for row in examples]
            self.v2 = await self.version(db, dataset.id)
            self.updated_revision = await self.revise(db, self.example_ids[0], self.v2, "PATCH")
            await self.revise(db, self.example_ids[1], self.v2, "DELETE")
        self.path = f"/api/v1/datasets/{self.dataset_id}/experiments"

    @staticmethod
    async def version(db, dataset_id):
        version = DatasetVersion(dataset_id=dataset_id)
        db.add(version)
        await db.flush()
        return version.id

    @staticmethod
    async def revise(db, example_id, version_id, kind):
        revision = DatasetExampleRevision(dataset_example_id=example_id, dataset_version_id=version_id,
                                          revision_kind=kind, input={"version": version_id}, output={})
        db.add(revision)
        await db.flush()
        return revision.id

    def payload(self, **changes):
        return {"name": "  订单回归  ", "execution_config": {
            "runner_id": self.runner_id, "agent_target_id": "  order-agent  ",
        }, **changes}

    async def create_experiment(self, **changes):
        response = await self.client.post(self.path, json=self.payload(**changes))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["location"], f"/api/v1/experiments/{response.json()['experiment_id']}")
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)

    async def test_create_locks_version_and_revisions_and_queues_repetitions_without_dispatch(self):
        older = (await self.create_experiment(dataset_version_id=self.v1)).json()
        async with self.database.transaction() as db:
            foreign = Dataset(name="其他数据集", user_id=self.owner_id)
            db.add(foreign)
            await db.flush()
            foreign_version = await self.version(db, foreign.id)
            # 现有 ORM 的两个独立外键允许这种不一致的历史记录；快照不能选它。
            await self.revise(db, self.example_ids[2], foreign_version, "PATCH")
            newest = await self.version(db, self.dataset_id)
        created = await self.create_experiment(repetitions=2, metadata={"tags": ["回归", None]})
        detail = created.json()
        self.assertEqual(detail["dataset_version_id"], newest)
        self.assertEqual(detail["name"], "订单回归")
        self.assertEqual(detail["execution_config"], {
            "runner_id": self.runner_id, "agent_target_id": "order-agent", "timeout_seconds": 60,
        })
        self.assertEqual(detail["metadata"], {"tags": ["回归", None]})
        self.assertEqual(detail["status"], "pending")
        self.assertEqual(detail["progress"], {"example_count": 2, "job_count": 4, "queued": 4,
                                             "assigned": 0, "running": 0, "succeeded": 0,
                                             "failed": 0, "cancelled": 0, "unknown": 0})
        self.assertTrue(detail["created_at"].endswith("Z"))
        async with self.database.transaction() as db:
            links = ExperimentDatasetExample
            snapshot = {example_id: revision_id for example_id, revision_id in (await db.execute(
                select(links.dataset_example_id, links.dataset_example_revision_id)
                .where(links.experiment_id == detail["experiment_id"]))).all()}
            self.assertEqual(snapshot, {self.example_ids[0]: self.updated_revision,
                                        self.example_ids[2]: self.original_revisions[2]})
            original = {example_id: revision_id for example_id, revision_id in (await db.execute(
                select(links.dataset_example_id, links.dataset_example_revision_id)
                .where(links.experiment_id == older["experiment_id"]))).all()}
            self.assertEqual(original, dict(zip(self.example_ids, self.original_revisions)))
            jobs = (await db.scalars(select(ExperimentRun).where(ExperimentRun.experiment_id == detail["experiment_id"]))).all()
            self.assertEqual({(row.dataset_example_id, row.repetition_number) for row in jobs},
                             {(example_id, number) for example_id in snapshot for number in (1, 2)})
            self.assertEqual(len({row.job_id for row in jobs}), 4)
            self.assertTrue(all(row.status == "queued" and row.output is None for row in jobs))
            self.assertEqual(await db.scalar(select(func.count()).select_from(RunnerAttempt)), 0)
            future = await self.version(db, self.dataset_id)
            await self.revise(db, self.example_ids[0], future, "DELETE")
        response = await self.client.get(created.headers["location"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), detail)

    async def test_list_orders_and_pages_across_versions_and_aggregates_job_states(self):
        self.assertEqual((await self.client.get(self.path)).json(), {"items": [], "page": 1, "pagesize": 20, "total": 0})
        first = (await self.create_experiment(dataset_version_id=self.v1)).json()
        second = (await self.create_experiment()).json()
        async with self.database.transaction() as db:
            await db.execute(update(Experiment).values(created_at=datetime(2026, 11, 8, tzinfo=timezone.utc)))
            rows = (await db.scalars(select(ExperimentRun).where(ExperimentRun.experiment_id == second["experiment_id"])
                                    .order_by(ExperimentRun.id))).all()
            rows[0].status, rows[0].output = "succeeded", {"task_output": None}
            rows[1].status = "unknown"
            experiment = await db.get(Experiment, second["experiment_id"])
            experiment.execution_config = {**experiment.execution_config, "url": "private-url", "headers": {"token": "secret"}}
        p1 = (await self.client.get(self.path, params={"page": 1, "pagesize": 1})).json()
        p2 = (await self.client.get(self.path, params={"page": 2, "pagesize": 1})).json()
        empty = (await self.client.get(self.path, params={"page": 3, "pagesize": 1})).json()
        self.assertEqual([p1["items"][0]["experiment_id"], p2["items"][0]["experiment_id"]],
                         [second["experiment_id"], first["experiment_id"]])
        self.assertEqual(empty, {"items": [], "page": 3, "pagesize": 1, "total": 2})
        item = p1["items"][0]
        self.assertEqual((item["progress"]["example_count"], item["progress"]["job_count"]), (2, 2))
        self.assertEqual((item["progress"]["queued"], item["progress"]["succeeded"], item["progress"]["unknown"]), (0, 1, 1))
        self.assertEqual(item["status"], "pending")
        self.assertTrue({"description", "metadata", "execution_config", "runs", "attempts"}.isdisjoint(item))
        detail = (await self.client.get(f"/api/v1/experiments/{second['experiment_id']}")).json()
        self.assertEqual(detail["execution_config"], second["execution_config"])
        self.assertEqual(detail["progress"], item["progress"])

    async def test_unauthenticated_requests_and_create_csrf_origin(self):
        anonymous = self.new_client()
        for response in (await anonymous.post(self.path, json=self.payload()),
                         await anonymous.get(self.path), await anonymous.get("/api/v1/experiments/1")):
            self.assert_error(response, 401, "not_authenticated")
        self.assert_error(await self.client.post(self.path, json=self.payload(), headers={"X-CSRF-Token": "bad"}),
                          403, "csrf_invalid")
        self.assert_error(await self.client.post(self.path, json=self.payload(), headers={"Origin": "https://other.example"}),
                          403, "origin_forbidden")

    async def test_dataset_experiment_version_and_runner_ownership(self):
        detail = await self.create_experiment()
        bob = self.new_client()
        await self.register("bob", bob)
        await self.login(bob, username="bob")
        bob_runner = (await bob.post("/api/v1/runners", json={"name": "其他用户"})).json()["runner"]["runner_id"]
        self.assert_error(await bob.post(self.path, json=self.payload()), 404, "dataset_not_found")
        self.assert_error(await bob.get(self.path), 404, "dataset_not_found")
        self.assert_error(await bob.get(detail.headers["location"]), 404, "experiment_not_found")
        async with self.database.transaction() as db:
            other = Dataset(name="另一个数据集", user_id=self.owner_id)
            ownerless = Dataset(name="没有归属的数据集")
            db.add_all([other, ownerless])
            await db.flush()
            other_version = await self.version(db, other.id)
            ownerless_id = ownerless.id
        self.assert_error(await self.client.get(f"/api/v1/datasets/{ownerless_id}/experiments"), 404, "dataset_not_found")
        self.assert_error(await self.client.post(self.path, json=self.payload(dataset_version_id=other_version)),
                          404, "dataset_version_not_found")
        self.assert_error(await self.client.post(self.path, json=self.payload(execution_config={
            "runner_id": bob_runner, "agent_target_id": "order-agent",
        })), 404, "runner_not_found")
        async with self.database.transaction() as db:
            await db.execute(update(Runner).where(Runner.runner_id == self.runner_id).values(enabled=False))
        self.assert_error(await self.client.post(self.path, json=self.payload()), 409, "runner_disabled")
        self.assertEqual((await self.client.get(self.path)).json()["total"], 1)

    async def test_request_validation_rejects_reserved_fields_invalid_ids_and_nonfinite_numbers(self):
        for changes in ({"status": "running"}, {"user_id": self.owner_id}, {"dataset_id": self.dataset_id},
                        {"name": "  "}, {"dataset_version_id": None}, {"repetitions": True}, {"repetitions": 101},
                        {"metadata": []}, {"description": "\x00"}, {"execution_config": {
                            "runner_id": self.runner_id, "agent_target_id": "agent", "url": "http://localhost/",
                        }}, {"execution_config": {"runner_id": self.runner_id, "agent_target_id": "agent", "timeout_seconds": True}}):
            with self.subTest(changes=changes):
                self.assert_error(await self.client.post(self.path, json=self.payload(**changes)), 422, "invalid_request")
        response = await self.client.post(self.path, content='{"name":"bad","metadata":{"nested":[1e999]},'
                                          '"execution_config":{"runner_id":"x","agent_target_id":"y"}}',
                                          headers={"Content-Type": "application/json"})
        self.assert_error(response, 422, "invalid_request")
        for path in (self.path + "?page=0", self.path + "?pagesize=101", "/api/v1/experiments/2147483648"):
            self.assert_error(await self.client.get(path), 422, "invalid_request")

    async def test_empty_snapshot_and_job_limit_leave_no_records(self):
        with patch("evalforge.server.services.experiments.MAX_EXPERIMENT_JOBS", 3):
            self.assert_error(await self.client.post(self.path, json=self.payload(repetitions=2)),
                              422, "experiment_job_limit_exceeded")
        async with self.database.transaction() as db:
            empty = Dataset(name="无版本", user_id=self.owner_id)
            db.add(empty)
            await db.flush()
            empty_path = f"/api/v1/datasets/{empty.id}/experiments"
            deleted = await self.version(db, self.dataset_id)
            for example_id in (self.example_ids[0], self.example_ids[2]):
                await self.revise(db, example_id, deleted, "DELETE")
        self.assert_error(await self.client.post(empty_path, json=self.payload()), 404, "dataset_version_not_found")
        self.assert_error(await self.client.post(self.path, json=self.payload()), 422, "empty_experiment_dataset")
        self.assertEqual((await self.client.get(self.path)).json()["total"], 0)

    async def test_job_insert_failure_rolls_back_experiment_and_snapshot(self):
        engine = self.database.engine.sync_engine

        def fail_job_insert(_conn, _cursor, statement, params, _context, _many):
            if statement.lstrip().upper().startswith("INSERT INTO EXPERIMENT_RUNS"):
                raise OperationalError(statement, params, RuntimeError("private failure details"))

        event.listen(engine, "before_cursor_execute", fail_job_insert)
        try:
            response = await self.client.post(self.path, json=self.payload())
        finally:
            event.remove(engine, "before_cursor_execute", fail_job_insert)
        self.assert_error(response, 503, "service_unavailable")
        self.assertNotIn("private failure details", response.text)
        async with self.database.transaction() as db:
            for table in (Experiment, ExperimentDatasetExample, ExperimentRun):
                self.assertEqual(await db.scalar(select(func.count()).select_from(table)), 0)
        await self.create_experiment()
