"""自定义评分器管理、发布历史和租户隔离的接口测试。"""

import base64
import json
from dataclasses import replace

import httpx
from sqlalchemy import func, select

from api_support import APITestCase
from evalforge.db.models import CodeEvaluator, CodeEvaluatorVersion
from evalforge.server.app import create_app
from evalforge.server.evaluators.snekbox import SnekboxClient

ROOT = "/api/v1/evaluators/code"
SOURCE = "def evaluate(context):\n    return {'score': 1}"


class CodeEvaluatorAPITests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)

    async def create_evaluator(self, name):
        response = await self.client.post(ROOT, json={"name": name, "description": "测试"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    async def test_snekbox_http_client_lifecycle(self):
        def no_http(request):
            self.fail(f"Lifespan must not contact Snekbox: {request.url}")

        app = create_app(replace(self.config, snekbox_url="http://snekbox.test"),
                         database=self.database, redis_client=self.redis,
                         snekbox_transport=httpx.MockTransport(no_http))
        async with app.router.lifespan_context(app):
            client = app.state.snekbox.http
            self.assertFalse(client.is_closed)
            self.assertIs(app.state.scheduler.evaluations.sandbox, app.state.snekbox)
        self.assertTrue(client.is_closed)

    async def test_preview_translates_snekbox_requests_and_returns_its_result_without_writes(self):
        path = ROOT + "/preview"
        payload = {"source_code": SOURCE, "context": {
            "input": {}, "reference": {}, "output": None, "metadata": {},
        }}
        self.assert_error(await self.client.post(path, json=payload), 503, "sandbox_unavailable")
        forwarded = []

        def sandbox(request):
            self.assertEqual((request.method, str(request.url)), ("POST", "http://snekbox.test/eval"))
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("x-csrf-token", request.headers)
            body = json.loads(request.content)
            self.assertEqual(body["args"], ["main.py"])
            files = {file["path"]: base64.b64decode(file["content"]) for file in body["files"]}
            self.assertEqual(set(files), {"main.py", "evaluator.py", "context.json"})
            self.assertIn(b"evaluate(context)", files["main.py"])
            self.assertEqual(files["evaluator.py"].decode(), SOURCE)
            forwarded.append(json.loads(files["context.json"]))
            return httpx.Response(200, json={
                "returncode": 0, "stdout": "a log line",
                "files": [{"path": "result.json", "content": base64.b64encode(b'{"score":0.25}').decode()}],
            })

        http = httpx.AsyncClient(base_url="http://snekbox.test", transport=httpx.MockTransport(sandbox))
        self.addAsyncCleanup(http.aclose)
        self.app.state.snekbox = SnekboxClient(http)
        response = await self.client.post(path, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        outcome = response.json()
        self.assertGreaterEqual(outcome.pop("duration_ms"), 0)
        self.assertEqual(outcome, {
            "status": "succeeded", "simulated": False,
            "result": {"score": 0.25, "label": None, "explanation": None},
            "stdout": "a log line",
        })
        for invalid in (
            {**payload, "source_code": " \n"}, {**payload, "source_code": "x" * 100_001},
            {**payload, "context": {"input": {}, "reference": {}, "metadata": {}}},
            {**payload, "context": {**payload["context"], "input": []}},
            {**payload, "context": {**payload["context"], "extra": 1}},
            {**payload, "context": {**payload["context"], "output": "x" * (256 * 1024)}},
        ):
            with self.subTest(fields=list(invalid)):
                self.assert_error(await self.client.post(path, json=invalid), 422, "invalid_request")
        self.assert_error(await self.new_client().post(path, json=payload), 401, "not_authenticated")
        self.client.headers.pop("X-CSRF-Token")
        self.assert_error(await self.client.post(path, json=payload), 403, "csrf_invalid")
        self.assertEqual(forwarded, [payload["context"]])
        async with self.database.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(CodeEvaluator)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(CodeEvaluatorVersion)), 0)

    async def test_preview_distinguishes_execution_errors_from_unavailable_sandbox(self):
        payload = {"source_code": SOURCE, "context": {"input": {}, "reference": {}, "output": [], "metadata": {}}}
        valid = {"returncode": 0, "stdout": "", "files": []}

        def encoded(contents):
            return {"path": "result.json", "content": base64.b64encode(contents).decode()}

        for upstream, status, code in (
            (httpx.Response(200, json={**valid, "returncode": 1}), 200, "evaluator_execution_failed"),
            (httpx.Response(200, json={**valid, "returncode": 137}), 200, "sandbox_resource_limit"),
            (httpx.Response(200, json={**valid, "files": [encoded(b'{"score":true}')]}), 200, "invalid_evaluator_result"),
            (httpx.Response(200, json={**valid, "files": [encoded(b'{"score":NaN}')]}), 200, "invalid_evaluator_result"),
            (httpx.Response(200, json=valid), 200, "invalid_evaluator_result"),
            (httpx.Response(200, json={**valid, "returncode": 255}), 503, "sandbox_unavailable"),
            (httpx.Response(200, json={}), 503, "sandbox_unavailable"),
            (httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)), 503, "sandbox_unavailable"),
            (httpx.Response(502), 503, "sandbox_unavailable"),
        ):
            with self.subTest(status=status, code=code):
                async with httpx.AsyncClient(base_url="http://snekbox.test",
                                             transport=httpx.MockTransport(lambda _: upstream)) as http:
                    self.app.state.snekbox = SnekboxClient(http)
                    response = await self.client.post(ROOT + "/preview", json=payload)
                self.assert_error(response, status, code)
                if status == 200:
                    self.assertEqual(response.json()["status"], "failed")
                    self.assertIsNone(response.json()["result"])

        def timeout(request):
            raise httpx.ReadTimeout("simulated upstream timeout", request=request)

        async with httpx.AsyncClient(base_url="http://snekbox.test", transport=httpx.MockTransport(timeout)) as http:
            self.app.state.snekbox = SnekboxClient(http)
            self.assert_error(await self.client.post(ROOT + "/preview", json=payload), 503, "sandbox_unavailable")

    async def test_draft_publish_and_archive_preserves_history(self):
        evaluator = await self.create_evaluator(" 状态检查 ")
        self.assertEqual(evaluator["name"], "状态检查")
        self.assertIsNone(evaluator["draft_source_code"])
        self.assertIsNone(evaluator["latest_version"])
        self.assertIsNone(evaluator["archived_at"])
        path = f"{ROOT}/{evaluator['id']}"
        versions = path + "/versions"
        self.assert_error(await self.client.post(versions), 409, "draft_required")
        for source in ("  \n", SOURCE):
            saved = await self.client.patch(path + "/draft", json={"source_code": source})
            self.assertEqual(saved.json(), {"draft_source_code": source})
            if source != SOURCE:
                self.assert_error(await self.client.post(versions), 409, "draft_required")
        first = await self.client.post(versions)
        self.assertEqual(first.status_code, 201, first.text)
        changed = "# 下一版\n" + SOURCE
        await self.client.patch(path + "/draft", json={"source_code": changed})
        second = await self.client.post(versions)
        self.assertEqual(second.status_code, 201, second.text)
        first, second = first.json(), second.json()
        self.assertEqual([first["version_number"], second["version_number"]], [1, 2])

        detail = await self.client.patch(path, json={"name": "状态一致性", "description": None})
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json(), {"id": evaluator["id"], "name": "状态一致性", "description": None,
                                         "draft_source_code": changed, "latest_version": second, "archived_at": None})
        self.assertEqual((await self.client.get(path)).json(), detail.json())
        page = (await self.client.get(versions, params={"pagesize": 1})).json()
        self.assertEqual(page, {"items": [second], "page": 1, "pagesize": 1, "total": 2})
        first_detail = (await self.client.get(f"{versions}/{first['id']}")).json()
        self.assertEqual(first_detail, {**first, "source_code": SOURCE})

        unpublished = await self.create_evaluator("尚无发布版本")
        unfiltered = (await self.client.get(ROOT, params={"pagesize": 1})).json()
        self.assertEqual(unfiltered["total"], 2)
        self.assertEqual(unfiltered["items"][0]["id"], unpublished["id"])
        listing = (await self.client.get(ROOT, params={"published_only": "true"})).json()
        self.assertEqual(listing["items"], [{key: value for key, value in detail.json().items()
                                            if key != "draft_source_code"}])

        archived = await self.client.post(path + "/archive")
        self.assertEqual(archived.status_code, 200, archived.text)
        archived_detail = archived.json()
        archived_at = archived_detail["archived_at"]
        self.assertIsNotNone(archived_at)
        self.assertEqual(archived_detail, {**detail.json(), "archived_at": archived_at})
        self.assertEqual((await self.client.post(path + "/archive")).json(), archived_detail)
        self.assertEqual((await self.client.get(path)).json(), archived_detail)
        self.assertEqual((await self.client.get(versions)).json()["items"], [second, first])
        self.assertEqual((await self.client.get(f"{versions}/{first['id']}")).json(), first_detail)

        active = (await self.client.get(ROOT)).json()
        self.assertEqual(active["total"], 1)
        self.assertEqual([row["id"] for row in active["items"]], [unpublished["id"]])
        all_items = (await self.client.get(ROOT, params={"include_archived": True})).json()
        self.assertEqual(all_items["total"], 2)
        self.assertEqual(all_items["items"][1]["archived_at"], archived_at)
        for include in (False, True):
            selectable = (await self.client.get(ROOT, params={"published_only": True, "include_archived": include})).json()
            self.assertEqual(selectable["total"], 0)
            self.assertEqual(selectable["items"], [])

        self.assert_error(await self.client.patch(path, json={"name": "禁止修改"}), 409, "code_evaluator_archived")
        self.assert_error(await self.client.patch(path + "/draft", json={"source_code": SOURCE}),
                          409, "code_evaluator_archived")
        self.assert_error(await self.client.post(versions), 409, "code_evaluator_archived")
        self.assertEqual((await self.client.delete(path)).status_code, 405)
        async with self.database.transaction() as db:
            row = await db.get(CodeEvaluator, evaluator["id"])
            self.assertEqual(row.draft_source_code, changed)
            self.assertIsNotNone(row.archived_at)
            self.assertEqual(await db.scalar(select(func.count()).select_from(CodeEvaluatorVersion)), 2)

    async def test_auth_ownership_and_name_conflicts(self):
        first = await self.create_evaluator("私有定义")
        second = await self.create_evaluator("另一个定义")
        path = f"{ROOT}/{first['id']}"
        await self.client.patch(path + "/draft", json={"source_code": SOURCE})
        version = (await self.client.post(path + "/versions")).json()
        self.assert_error(await self.client.get(f"{ROOT}/{second['id']}/versions/{version['id']}"),
                          404, "code_evaluator_version_not_found")
        self.assert_error(await self.client.patch(path, json={"name": second["name"]}),
                          409, "evaluator_name_conflict")
        self.assert_error(await self.client.post(ROOT, json={"name": "exact_match"}),
                          409, "evaluator_name_conflict")
        self.assert_error(await self.client.patch(path, json={"name": "   "}), 422, "invalid_request")
        self.assert_error(await self.new_client().get(ROOT), 401, "not_authenticated")
        other = self.new_client()
        await self.register("bob", other)
        await self.login(other, username="bob")
        self.assertEqual((await other.get(ROOT)).json()["items"], [])
        for method, target, fields in (
            ("GET", path, None), ("GET", f"{path}/versions/{version['id']}", None),
            ("PATCH", path, {"name": "越权"}), ("PATCH", path + "/draft", {"source_code": ""}),
            ("POST", path + "/versions", None), ("POST", path + "/archive", None),
        ):
            self.assert_error(await other.request(method, target, json=fields), 404, "code_evaluator_not_found")
        self.assert_error(await other.post(ROOT, json={"name": first["name"]}), 409, "evaluator_name_conflict")
        self.client.headers.pop("X-CSRF-Token")
        self.assert_error(await self.client.post(path + "/archive"), 403, "csrf_invalid")
