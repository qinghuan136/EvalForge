"""Runner API 行为测试：归属、唯一凭证、分页、逻辑删除和依赖失败。"""

import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SyncSession

from api_support import APITestCase
from evalforge.db.models import (
    Dataset, DatasetExample, DatasetVersion, Experiment, ExperimentRun, Runner, RunnerAttempt, RunnerCredential,
)
from evalforge.server.runner_connections import RUNNER_SESSION_PREFIX
from evalforge.server.config import ServerSettings


class RunnerAPITests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()

    async def create_runner(self, name="开发机", client=None):
        client = client or self.client
        response = await client.post("/api/v1/runners", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    async def test_creation_issues_one_ten_year_credential_and_stores_only_secret_digest(self):
        data = await self.create_runner("  我的开发机  ")
        runner_id = data["runner"]["runner_id"]
        token = data["credential"]["token"]
        prefix, lookup, secret = token.split(".")
        self.assertEqual(prefix, "efr")
        self.assertEqual(lookup, data["credential"]["token_id"])
        self.assertGreaterEqual(len(secret), 43)
        self.assertEqual(data["runner"]["name"], "我的开发机")
        self.assertEqual(data["connection"]["websocket_url"], self.config.runner_websocket_url)
        created = datetime.fromisoformat(data["runner"]["created_at"])
        expires = datetime.fromisoformat(data["credential"]["expires_at"])
        self.assertEqual(expires, created.replace(year=created.year + 10))
        async with self.database.transaction() as db:
            row = await db.get(Runner, runner_id)
            credential = await db.scalar(select(RunnerCredential).where(RunnerCredential.runner_id == runner_id))
            self.assertEqual(row.owner_user_id, 1)
            self.assertIsNone(row.deleted_at)
            self.assertEqual(credential.id, data["credential"]["id"])
            self.assertEqual(credential.token_hash, hashlib.sha256(secret.encode()).digest())
            self.assertIsNone(credential.revoked_at)
        detail = await self.client.get(f"/api/v1/runners/{runner_id}")
        listing = await self.client.get("/api/v1/runners")
        for response in (detail, listing):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(token, response.text)
            self.assertNotIn("token_hash", response.text)
            self.assertNotIn("credential", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_pagination_is_stable_excludes_deleted_and_other_owners(self):
        created = [await self.create_runner(str(number)) for number in range(4)]
        deleted_id = created[0]["runner"]["runner_id"]
        self.assertEqual((await self.client.delete(f"/api/v1/runners/{deleted_id}")).status_code, 204)
        bob = self.new_client()
        await self.register("bob", bob)
        await self.login(bob, username="bob")
        await self.create_runner("bob-only", bob)
        async with self.database.transaction() as db:
            await db.execute(update(Runner).where(Runner.owner_user_id == 1)
                             .values(created_at=datetime(2026, 11, 4, tzinfo=timezone.utc)))
        expected = sorted([item["runner"]["runner_id"] for item in created[1:]], reverse=True)
        first = (await self.client.get("/api/v1/runners?page=1&pagesize=2")).json()
        second = (await self.client.get("/api/v1/runners?page=2&pagesize=2")).json()
        self.assertEqual(first["total"], 3)
        self.assertEqual(second["total"], 3)
        self.assertEqual((first["page"], first["pagesize"]), (1, 2))
        self.assertEqual([row["runner_id"] for row in first["items"] + second["items"]], expected)
        beyond = (await self.client.get("/api/v1/runners?page=5&pagesize=2")).json()
        self.assertEqual(beyond["items"], [])
        self.assertEqual(beyond["total"], 3)
        defaults = (await self.client.get("/api/v1/runners")).json()
        self.assertEqual((defaults["page"], defaults["pagesize"]), (1, 20))

    async def test_connection_snapshots_are_batched_and_missing_records_mean_offline(self):
        first, second = await self.create_runner("one"), await self.create_runner("two")
        first_id, second_id = first["runner"]["runner_id"], second["runner"]["runner_id"]
        await self.redis.set(RUNNER_SESSION_PREFIX + first_id, json.dumps({"session_id": "session-1", "phase": "ready"}), ex=60)
        with patch.object(self.redis, "mget", wraps=self.redis.mget) as batch:
            response = await self.client.get("/api/v1/runners")
        self.assertEqual(response.status_code, 200, response.text)
        batch.assert_called_once()
        states = {row["runner_id"]: row["connection"] for row in response.json()["items"]}
        self.assertEqual(states[first_id], {"online": True, "phase": "ready"})
        self.assertEqual(states[second_id], {"online": False, "phase": "disconnected"})

    async def test_rename_only_changes_name_and_preserves_credential(self):
        created = await self.create_runner()
        runner_id = created["runner"]["runner_id"]
        response = await self.client.patch(f"/api/v1/runners/{runner_id}", json={"name": "  新名字  "})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "新名字")
        self.assertNotIn("credential", response.text)
        self.assertTrue(response.json()["updated_at"].endswith("Z"))
        async with self.database.transaction() as db:
            credential = await db.get(RunnerCredential, created["credential"]["id"])
            self.assertEqual(credential.token_id, created["credential"]["token_id"])
            self.assertIsNone(credential.revoked_at)

    async def test_all_operations_are_owner_scoped(self):
        created = await self.create_runner()
        path = f"/api/v1/runners/{created['runner']['runner_id']}"
        bob = self.new_client()
        await self.register("bob", bob)
        await self.login(bob, username="bob")
        for method, body in (("GET", None), ("PATCH", {"name": "hijacked"}), ("DELETE", None)):
            response = await bob.request(method, path, json=body)
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["error"]["code"], "runner_not_found")
        self.assertEqual((await bob.get("/api/v1/runners")).json()["items"], [])
        self.assertEqual((await self.client.get(path)).json()["name"], "开发机")

    async def test_browser_login_and_csrf_are_required(self):
        created = await self.create_runner()
        path = f"/api/v1/runners/{created['runner']['runner_id']}"
        anonymous = self.new_client()
        self.assertEqual((await anonymous.get("/api/v1/runners")).status_code, 401)
        await self.csrf(anonymous)
        self.assertEqual((await anonymous.post("/api/v1/runners", json={"name": "anonymous"})).status_code, 401)
        for method, target, body in (("POST", "/api/v1/runners", {"name": "new"}),
                                     ("PATCH", path, {"name": "new"}), ("DELETE", path, None)):
            for headers in ({"X-CSRF-Token": ""}, {"Origin": "https://evil.example"}):
                response = await self.client.request(method, target, json=body, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)

    async def test_names_pagination_and_immutable_fields_are_validated(self):
        created = await self.create_runner()
        path = f"/api/v1/runners/{created['runner']['runner_id']}"
        for body in ({"name": " "}, {"name": "x" * 101}, {"name": None}, {"name": 123},
                     {"name": "new", "enabled": False}, {"name": "new", "owner_user_id": 2},
                     {"name": "new", "token": "private-secret"}):
            for method, target in (("POST", "/api/v1/runners"), ("PATCH", path)):
                response = await self.client.request(method, target, json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("private-secret", response.text)
        for query in ("page=0", "page=-1", "page=bad", "page=99999999999999999999", "pagesize=0", "pagesize=101"):
            response = await self.client.get("/api/v1/runners?" + query)
            self.assertEqual(response.status_code, 422)

    async def test_delete_revokes_credential_preserves_attempt_history_and_can_recreate(self):
        created = await self.create_runner()
        runner_id = created["runner"]["runner_id"]
        path = f"/api/v1/runners/{runner_id}"
        async with self.database.transaction() as db:
            dataset = Dataset(name="dataset")
            experiment = Experiment(dataset=dataset, dataset_version=DatasetVersion(dataset=dataset),
                                    user_id=1, name="test", repetitions=1)
            run = ExperimentRun(experiment=experiment, dataset_example=DatasetExample(dataset=dataset), repetition_number=1)
            attempt = RunnerAttempt(experiment_run=run, runner_id=runner_id, dispatch_json={}, status="accepted")
            db.add(attempt)
            await db.flush()
            attempt_id = attempt.id
        await self.redis.set(RUNNER_SESSION_PREFIX + runner_id, '{"session_id":"s1","phase":"ready"}', ex=60)
        response = await self.client.delete(path)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        self.assertIsNone(await self.redis.get(RUNNER_SESSION_PREFIX + runner_id))
        async with self.database.transaction() as db:
            runner = await db.get(Runner, runner_id)
            credential = await db.get(RunnerCredential, created["credential"]["id"])
            self.assertFalse(runner.enabled)
            self.assertIsNotNone(runner.deleted_at)
            self.assertIsNotNone(credential.revoked_at)
            deleted_at, revoked_at = runner.deleted_at, credential.revoked_at
            self.assertEqual((await db.get(RunnerAttempt, attempt_id)).status, "accepted")
        self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual((await self.client.patch(path, json={"name": "restore"})).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/runners")).json()["total"], 0)
        self.assertEqual((await self.client.delete(path)).status_code, 204)
        async with self.database.transaction() as db:
            self.assertEqual((await db.get(Runner, runner_id)).deleted_at, deleted_at)
            self.assertEqual((await db.get(RunnerCredential, created["credential"]["id"])).revoked_at, revoked_at)
        rebuilt = await self.create_runner()
        self.assertNotEqual(rebuilt["runner"]["runner_id"], runner_id)
        self.assertNotEqual(rebuilt["credential"]["token"], created["credential"]["token"])

    async def test_redis_cleanup_failure_is_retryable_after_committed_revocation(self):
        created = await self.create_runner()
        runner_id = created["runner"]["runner_id"]
        path = f"/api/v1/runners/{runner_id}"
        await self.redis.set(RUNNER_SESSION_PREFIX + runner_id, '{"session_id":"s1","phase":"ready"}', ex=60)
        with patch.object(self.app.state.runners.connections, "invalidate", side_effect=RedisConnectionError("private failure")):
            response = await self.client.delete(path)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        async with self.database.transaction() as db:
            self.assertIsNotNone((await db.get(Runner, runner_id)).deleted_at)
            self.assertIsNotNone((await db.get(RunnerCredential, created["credential"]["id"])).revoked_at)
        self.assertEqual((await self.client.delete(path)).status_code, 204)
        self.assertIsNone(await self.redis.get(RUNNER_SESSION_PREFIX + runner_id))

    async def test_connection_store_failure_is_not_silently_reported_as_offline(self):
        created = await self.create_runner()
        runner_id = created["runner"]["runner_id"]
        with patch.object(self.redis, "mget", side_effect=RedisConnectionError("private failure")):
            for path in ("/api/v1/runners", f"/api/v1/runners/{runner_id}"):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private", response.text)
        await self.redis.set(RUNNER_SESSION_PREFIX + runner_id, "invalid-json", ex=60)
        response = await self.client.get(f"/api/v1/runners/{runner_id}")
        self.assertEqual(response.status_code, 503)

    async def test_rename_can_be_retried_after_connection_lookup_failure(self):
        created = await self.create_runner()
        path = f"/api/v1/runners/{created['runner']['runner_id']}"
        with patch.object(self.redis, "mget", side_effect=RedisConnectionError("private failure")):
            response = await self.client.patch(path, json={"name": "renamed"})
        self.assertEqual(response.status_code, 503)
        async with self.database.transaction() as db:
            self.assertEqual((await db.get(Runner, created["runner"]["runner_id"])).name, "renamed")
        response = await self.client.patch(path, json={"name": "renamed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "renamed")

    async def test_database_commit_failure_returns_no_credential_and_rolls_back_creation(self):
        def fail_creation(session):
            if any(isinstance(row, RunnerCredential) for row in session.identity_map.values()):
                raise OperationalError("private database failure", {}, Exception("private failure"))

        event.listen(SyncSession, "before_commit", fail_creation)
        try:
            response = await self.client.post("/api/v1/runners", json={"name": "failed"})
        finally:
            event.remove(SyncSession, "before_commit", fail_creation)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("efr.", response.text)
        self.assertNotIn("private", response.text)
        self.assertEqual((await self.client.get("/api/v1/runners")).json()["total"], 0)

    async def test_delete_commit_failure_does_not_revoke_or_clear_connection(self):
        created = await self.create_runner()
        runner_id = created["runner"]["runner_id"]
        await self.redis.set(RUNNER_SESSION_PREFIX + runner_id, '{"session_id":"s1","phase":"ready"}', ex=60)

        def fail_delete(session):
            if any(isinstance(row, Runner) and row.deleted_at is not None for row in session.identity_map.values()):
                raise OperationalError("private failure", {}, Exception("private failure"))

        event.listen(SyncSession, "before_commit", fail_delete)
        try:
            response = await self.client.delete(f"/api/v1/runners/{runner_id}")
        finally:
            event.remove(SyncSession, "before_commit", fail_delete)
        self.assertEqual(response.status_code, 503)
        async with self.database.transaction() as db:
            self.assertIsNone((await db.get(Runner, runner_id)).deleted_at)
            self.assertIsNone((await db.get(RunnerCredential, created["credential"]["id"])).revoked_at)
        self.assertIsNotNone(await self.redis.get(RUNNER_SESSION_PREFIX + runner_id))


class RunnerConfigurationTests(unittest.TestCase):
    def test_public_runner_url_is_explicit_and_insecure_ws_is_local_only(self):
        for url in ("https://cloud.example/ws", "ws://cloud.example/ws", "wss://user:secret@cloud.example/ws",
                    "wss://cloud.example/ws?token=secret", "wss://cloud.example/ws#fragment", "wss://cloud.example:0/ws"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ServerSettings(runner_websocket_url=url)
        with self.assertRaises(ValueError):
            ServerSettings(runner_websocket_url="ws://localhost:8000/api/v1/runner/ws")
        local = ServerSettings(allowed_origins=("http://localhost:8000",), cookie_secure=False,
                               runner_websocket_url="ws://localhost:8000/api/v1/runner/ws")
        self.assertTrue(local.runner_websocket_url.startswith("ws://localhost"))
