"""导入接口：样本格式、版本关系、身份校验和批量回滚。"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from api_support import APITestCase
from evalforge.db.models import Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion, Runner
from evalforge.server.runner_auth import RunnerIdentity
from evalforge.server.runner_connections import RegisteredConnection, RunnerConnections
from evalforge.server.services.dispatch import DispatchClaim, DispatchService
from evalforge.server.services.runtime_loader import RuntimeLoader

URL = "/api/v1/datasets/import"


class DatasetImportAPITests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()

    async def upload(self, content, *, format="jsonl", client=None, **fields):
        if isinstance(content, str):
            content = content.encode("utf-8")
        return await (client or self.client).post(
            URL, data={"name": "测试集", "format": format, **fields}, files={"file": (f"cases.{format}", content)},
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)

    async def assert_counts(self, *, datasets=0, samples=0):
        async with self.database.transaction() as db:
            for model, count in [(Dataset, datasets), (DatasetVersion, datasets),
                                 (DatasetExample, samples), (DatasetExampleRevision, samples)]:
                self.assertEqual(await db.scalar(select(func.count()).select_from(model)), count)

    async def test_csv_preserves_selected_column_names_values_and_version(self):
        response = await self.upload(
            '\ufeffquery,code,answer,category,ignore\r\n"你好,\r\n世界",00123,巴黎,地理,忽略\r\n'
            '再见,007,,问候,忽略\r\n',
            format="csv", input_keys=["query", "code"], output_keys=["answer"], metadata_keys=["category"],
            name="  问答用例  ", description="Agent 回归",
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        info = response.json()
        self.assertEqual(info["example_count"], 2)
        owner = await self.user()
        async with self.database.transaction() as db:
            dataset = await db.get(Dataset, info["dataset_id"])
            version = await db.get(DatasetVersion, info["dataset_version_id"])
            self.assertEqual((dataset.name, dataset.description, dataset.user_id), ("问答用例", "Agent 回归", owner.id))
            self.assertEqual((version.dataset_id, version.user_id), (dataset.id, owner.id))
            rows = (await db.scalars(select(DatasetExampleRevision).join(DatasetExample)
                                     .where(DatasetExample.dataset_id == dataset.id)
                                     .order_by(DatasetExample.id))).all()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row.revision_kind == "CREATE" and row.dataset_version_id == version.id for row in rows))
            self.assertEqual(rows[0].input, {"query": "你好,\r\n世界", "code": "00123"})
            self.assertEqual(rows[0].output, {"answer": "巴黎"})
            self.assertEqual(rows[0].metadata_, {"category": "地理"})
            self.assertEqual(rows[1].output, {"answer": ""})

    async def test_dataset_reads_are_owner_scoped_and_page_stably(self):
        path = "/api/v1/datasets"
        first = (await self.upload('{"input":{}}', name="first")).json()
        other = self.new_client()
        await self.register("bob", other)
        await self.login(other, username="bob")
        foreign = (await self.upload('{"input":{}}', name="foreign", client=other)).json()
        async with self.database.transaction() as db:
            original = await db.get(Dataset, first["dataset_id"])
            empty = Dataset(name="empty", user_id=original.user_id, created_at=original.created_at,
                            metadata_={"tags": ["demo"]})
            db.add(empty)
            await db.flush()
            empty_id = empty.id

        self.client.headers.pop("X-CSRF-Token")
        for page, expected in [(1, empty_id), (2, first["dataset_id"])]:
            response = await self.client.get(path, params={"page": page, "pagesize": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["total"], 2)
            item, = response.json()["items"]
            self.assertEqual(item["dataset_id"], expected)
            self.assertEqual(item["latest_version_id"], None if page == 1 else first["dataset_version_id"])
            self.assertNotIn("metadata", item)
            self.assertTrue(item["created_at"].endswith("Z"))

        for suffix in ("", "/examples"):
            self.assert_error(await self.client.get(f"{path}/{foreign['dataset_id']}{suffix}"),
                              404, "dataset_not_found")
        for suffix in ("", f"/{empty_id}", f"/{empty_id}/examples"):
            self.assert_error(await self.new_client().get(path + suffix), 401, "not_authenticated")
        self.assert_error(await self.client.get(path, params={"pagesize": 101}), 422, "invalid_request")
        self.assert_error(await self.client.get(f"{path}/{empty_id}/examples", params={"version_id": 0}),
                          422, "invalid_request")
        self.assert_error(await self.client.get(f"{path}/{empty_id}/examples",
                                               params={"version_id": foreign["dataset_version_id"]}),
                          404, "dataset_version_not_found")
        empty_detail = (await self.client.get(f"{path}/{empty_id}")).json()
        self.assertEqual((empty_detail["example_count"], empty_detail["metadata"]), (0, {"tags": ["demo"]}))
        self.assertEqual((await self.client.get(f"{path}/{empty_id}/examples")).json(), {
            "dataset_id": empty_id, "dataset_version_id": None, "items": [], "total": 0, "page": 1, "pagesize": 20,
        })

    async def test_dataset_examples_use_latest_revision_at_selected_version(self):
        created = (await self.upload('\n'.join(json.dumps({
            "input": {"n": n}, "output": {"answer": n}, "metadata": {"tag": "original"},
        }) for n in (1, 2, 3)))).json()
        dataset_id, v1 = created["dataset_id"], created["dataset_version_id"]
        async with self.database.transaction() as db:
            first, second, third = (await db.scalars(select(DatasetExample.id)
                .where(DatasetExample.dataset_id == dataset_id).order_by(DatasetExample.id))).all()
            v2 = DatasetVersion(dataset_id=dataset_id)
            db.add(v2)
            await db.flush()
            v2_id = v2.id
            db.add_all([
                DatasetExampleRevision(dataset_version_id=v2_id, dataset_example_id=first,
                                       revision_kind="PATCH", input={"n": 10}, output={"answer": 10},
                                       metadata_={"tag": "patched"}),
                DatasetExampleRevision(dataset_version_id=v2_id, dataset_example_id=second,
                                       revision_kind="DELETE", input={}, output={}),
            ])

        path = f"/api/v1/datasets/{dataset_id}"
        detail = (await self.client.get(path)).json()
        self.assertEqual((detail["latest_version_id"], detail["example_count"]), (v2_id, 2))
        old = (await self.client.get(path + "/examples", params={"version_id": v1})).json()
        self.assertEqual((old["dataset_version_id"], old["total"]), (v1, 3))
        self.assertEqual([item["example_id"] for item in old["items"]], [first, second, third])
        self.assertEqual(old["items"][0]["input"], {"n": 1})
        latest = (await self.client.get(path + "/examples", params={"pagesize": 1})).json()
        self.assertEqual((latest["dataset_version_id"], latest["total"]), (v2_id, 2))
        self.assertEqual(latest["items"], [{
            "example_id": first, "input": {"n": 10}, "output": {"answer": 10}, "metadata": {"tag": "patched"},
        }])
        next_page = (await self.client.get(path + "/examples", params={"page": 2, "pagesize": 1})).json()
        self.assertEqual([item["example_id"] for item in next_page["items"]], [third])

    async def test_jsonl_preserves_nested_objects_through_experiment_dispatch(self):
        sample = {"input": {"messages": [{"role": "user", "content": "你好"}], "max_tokens": 10, "verbose": False},
                  "output": {"answer": "你好", "citations": [{"source": 1}]},
                  "metadata": {"topic": "greeting"}}
        no_reference = {"input": {"messages": []}}
        response = await self.upload("\n\n".join(json.dumps(row, ensure_ascii=False) for row in (sample, no_reference)))
        self.assertEqual(response.status_code, 201, response.text)
        info = response.json()
        owner = await self.user()
        async with self.database.transaction() as db:
            first, second = (await db.scalars(select(DatasetExampleRevision).order_by(DatasetExampleRevision.id))).all()
            self.assertEqual((first.input, first.output, first.metadata_),
                             (sample["input"], sample["output"], sample["metadata"]))
            self.assertEqual((second.input, second.output, second.metadata_), (no_reference["input"], {}, {}))
            runner = Runner(name="开发机", owner_user_id=owner.id)
            db.add(runner)
            await db.flush()
        await RunnerConnections(self.redis).register(runner.runner_id, RegisteredConnection(
            session_id="test-session", phase="syncing", capabilities=["http_json"],
        ), 60)
        experiment = await self.client.post(f"/api/v1/datasets/{info['dataset_id']}/experiments", json={
            "name": "导入后运行",
            "dataset_version_id": info["dataset_version_id"],
            "execution_config": {"runner_id": runner.runner_id, "agent_target_id": "test-agent"},
        })
        self.assertEqual(experiment.status_code, 201, experiment.text)
        self.assertEqual(experiment.json()["progress"]["queued"], 2)
        identity = RunnerIdentity(runner.runner_id, owner.id, 1, datetime.max.replace(tzinfo=timezone.utc), b"")
        snapshot = await RuntimeLoader(self.database).load(identity)
        runtime = snapshot.experiments[experiment.json()["experiment_id"]]
        claim = await DispatchService(self.database).claim(identity, runtime, runtime.reserve_task())
        self.assertIsInstance(claim, DispatchClaim)
        self.assertEqual(claim.payload["input"], sample["input"])
        self.assertNotIn("output", claim.payload)

    async def test_invalid_jsonl_reports_line_and_does_not_save_valid_prefix(self):
        for bad, field in [
            ('{"input": {}, "output": "巴黎"}', "output"),
            ('{"input": {"n": NaN}}', "input"),
            ('{"input": {"n": "\\u0000"}}', "input"),
            ('{"input": {"n": 1, "n": 2}}', "重复字段"),
            ('{"output": {}}', "input"),
        ]:
            with self.subTest(bad=bad):
                response = await self.upload('{"input":{"query":"第一条"}}\n\n' + bad)
                self.assert_error(response, 422, "invalid_dataset_file")
                self.assertIn("第 3 行", response.json()["error"]["message"])
                self.assertIn(field, response.json()["error"]["message"])
        await self.assert_counts()

    async def test_csv_rejects_ambiguous_columns_and_malformed_rows(self):
        for fields in [{"input_keys": ["missing"]}, {"input_keys": ["query"], "output_keys": ["query"]}, {}]:
            self.assert_error(await self.upload("query,answer\nhi,hello\n", format="csv", **fields),
                              422, "invalid_dataset_columns")
        response = await self.upload("query,answer\nhi,hello\nbye\n", format="csv", input_keys=["query"])
        self.assert_error(response, 422, "invalid_dataset_file")
        self.assertIn("第 3 行", response.json()["error"]["message"])
        self.assert_error(await self.upload("query\n", format="csv", input_keys=["query"]),
                          422, "empty_dataset_file")
        await self.assert_counts()

    async def test_upload_requires_login_and_csrf_and_enforces_size_limits(self):
        content = '{"input":{"query":"hi"}}'
        self.assert_error(await self.upload(content, client=self.new_client()), 401, "not_authenticated")
        csrf = self.client.headers.pop("X-CSRF-Token")
        self.assert_error(await self.upload(content), 403, "csrf_invalid")
        self.client.headers["X-CSRF-Token"] = csrf
        with patch("evalforge.server.routes.datasets.MAX_DATASET_UPLOAD_BYTES", 100):
            for size in (101, 70_000):
                self.assert_error(await self.upload(b" " * size), 413, "dataset_file_too_large")
        with patch("evalforge.server.dataset_import.MAX_DATASET_EXAMPLES", 1):
            self.assert_error(await self.upload(f"{content}\n{content}"), 413, "dataset_too_many_examples")
        self.assert_error(await self.upload(b"\xff"), 422, "invalid_dataset_encoding")
        await self.assert_counts()

    async def test_duplicate_name_does_not_append_and_database_failure_rolls_back(self):
        sample = '{"input":{},"output":{"answer":"hello"}}'
        first = await self.upload(sample)
        self.assertEqual(first.status_code, 201, first.text)
        self.assert_error(await self.upload(sample), 409, "dataset_name_conflict")

        def fail_revision_insert(_conn, _cursor, statement, params, _context, _many):
            if statement.lstrip().upper().startswith("INSERT INTO DATASET_EXAMPLE_REVISIONS"):
                raise OperationalError(statement, params, RuntimeError("private failure details"))

        engine = self.database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", fail_revision_insert)
        try:
            failed = await self.upload(sample, name="需要回滚")
        finally:
            event.remove(engine, "before_cursor_execute", fail_revision_insert)
        self.assert_error(failed, 503, "service_unavailable")
        self.assertNotIn("private failure details", failed.text)
        await self.assert_counts(datasets=1, samples=1)
