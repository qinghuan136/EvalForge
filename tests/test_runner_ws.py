"""真实 TLS + Uvicorn + WebSocket 测试；身份存储使用异步 SQLite。"""

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from redis.exceptions import ConnectionError as RedisConnectionError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from api_support import APITestCase, SQLiteDatabase
from attempt_support import seed_attempt, result_params
from evalforge.db.models import (
    Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion, Experiment, Job, Runner, RunnerAttempt,
    RunnerCredential, User,
)
from evalforge.server.experiment_events import PreviewTarget


class RunnerWebSocketTests(APITestCase):
    def make_database(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return SQLiteDatabase("sqlite+aiosqlite:///" + str(Path(temp.name) / "ws.sqlite"))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tls = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tls.cleanup)
        cls.cert = Path(cls.tls.name) / "localhost.pem"
        cls.key = Path(cls.tls.name) / "localhost-key.pem"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=1)).add_extension(
                    x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                    critical=False).sign(key, hashes.SHA256()))
        cls.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        cls.key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()
        response = await self.client.post("/api/v1/runners", json={"name": "tls-runner"})
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.runner_id, self.credential_id = data["runner"]["runner_id"], data["credential"]["id"]
        self.token = data["credential"]["token"]
        self.app.state.settings = replace(self.config, runner_session_heartbeat_seconds=0.05)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.addCleanup(self.listener.close)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, lifespan="off", ws="websockets-sansio", log_config=None, access_log=False,
            ssl_certfile=str(self.cert), ssl_keyfile=str(self.key), ws_max_size=1048576,
            ws_ping_interval=0.2, ws_ping_timeout=1,
        ))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.listener]))
        self.addAsyncCleanup(self.stop_server)
        async with asyncio.timeout(5):
            while not self.server.started:
                if self.server_task.done():
                    self.server_task.result()
                await asyncio.sleep(0.01)
        self.url = f"wss://localhost:{self.listener.getsockname()[1]}/api/v1/runner/ws"
        self.ssl = ssl.create_default_context(cafile=str(self.cert))

    async def stop_server(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 5)

    def connection(self, token=None, **kwargs):
        headers = {"Authorization": f"Bearer {self.token if token is None else token}"}
        headers.update(kwargs.pop("headers", {}))
        return connect(self.url + kwargs.pop("suffix", ""), ssl=self.ssl, proxy=None,
                       additional_headers=headers, **kwargs)

    async def hello(self, ws, request_id=1, *, capabilities=("http_json",)):
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "runner.hello", "id": request_id,
                                  "params": {"runner_id": self.runner_id, "version": "0.1.0",
                                             "capabilities": capabilities, "max_concurrent_jobs": 1}}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
        self.assertIn("result", reply, reply)
        return reply["result"]

    async def test_valid_credential_binds_identity_and_unknown_methods_are_not_faked(self):
        async with self.connection() as ws:
            await ws.send('{"jsonrpc":"2.0","method":"runner.hello","id":1}')
            reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
            self.assertEqual(reply["error"]["code"], -32602)
        async with self.database.transaction() as db:
            self.assertIsNotNone((await db.get(RunnerCredential, self.credential_id)).last_used_at)
        self.assertIsNone(await self.redis.get("evalforge:runner:session:" + self.runner_id))

    async def test_hello_registers_syncing_and_business_methods_validate_params(self):
        key = "evalforge:runner:session:" + self.runner_id
        async with self.connection() as ws:
            hello = await self.hello(ws)
            record = json.loads(await self.redis.get(key))
            self.assertEqual(record["session_id"], hello["session_id"])
            self.assertEqual(record["phase"], "syncing")
            response = await self.client.get(f"/api/v1/runners/{self.runner_id}")
            self.assertEqual(response.json()["connection"], {"online": True, "phase": "syncing"})
            for method in ("runner.sync", "runner.ready", "jobs.complete"):
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "id": method}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
                self.assertEqual(reply["error"]["code"], -32602)
        async with asyncio.timeout(2):
            while await self.redis.get(key) is not None:
                await asyncio.sleep(0.01)
        self.assertEqual((await self.client.get(f"/api/v1/runners/{self.runner_id}")).json()["connection"]["online"], False)

    async def test_new_connection_takes_over_without_old_cleanup_removing_new_registration(self):
        async with self.connection() as old:
            first = await self.hello(old)
            async with self.connection() as new:
                second = await self.hello(new)
                self.assertNotEqual(first["session_id"], second["session_id"])
                with self.assertRaises(ConnectionClosed) as caught:
                    await asyncio.wait_for(old.recv(), 2)
                self.assertEqual(caught.exception.rcvd.code, 1008)
                record = json.loads(await self.redis.get("evalforge:runner:session:" + self.runner_id))
                self.assertEqual(record["session_id"], second["session_id"])
                await new.send('{"jsonrpc":"2.0","method":"runner.sync","id":8}')
                self.assertEqual(json.loads(await asyncio.wait_for(new.recv(), 2))["error"]["code"], -32602)

    async def test_sync_ready_started_complete_and_reconnect_replay_over_tls(self):
        owner = await self.user()
        key = await seed_attempt(self.database, owner.id, self.runner_id)
        async def call(ws, method, params):
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": method}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
            self.assertIn("result", reply, reply)
            return reply["result"]
        async with self.connection() as ws:
            hello = await self.hello(ws)
            await call(ws, "runner.sync", {"runner_id": self.runner_id, "active_jobs": [{
                "job_id": key[0], "attempt_id": key[1], "status": "accepted"}], "pending_results": []})
            await call(ws, "runner.ready", {"session_id": hello["session_id"]})
            await call(ws, "jobs.started", {"job_id": key[0], "attempt_id": key[1]})
            self.assertEqual(await call(ws, "jobs.complete", result_params(key)), {"acknowledged": True})
        async with self.connection() as ws:
            hello = await self.hello(ws)
            sync = await call(ws, "runner.sync", {"runner_id": self.runner_id, "active_jobs": [],
                                                  "pending_results": [result_params(key)]})
            self.assertEqual(sync["jobs"][0]["action"], "retry_upload")
            await call(ws, "jobs.complete", result_params(key))
            await call(ws, "runner.ready", {"session_id": hello["session_id"]})

    async def test_created_experiment_dispatches_frozen_input_after_ready_and_completes(self):
        await self.assert_dispatches_frozen_input(stream_output=False)

    async def test_created_streaming_experiment_dispatches_with_streaming_flag_and_completes(self):
        await self.assert_dispatches_frozen_input(stream_output=True)

    async def assert_dispatches_frozen_input(self, *, stream_output):
        owner = await self.user()
        async with self.database.transaction() as db:
            dataset = Dataset(name="dispatch", user_id=owner.id)
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            example = DatasetExample(dataset_id=dataset_id)
            db.add(example)
            await db.flush()
            example_id = example.id
            versions = []
            for kind, value in (("CREATE", "frozen"), ("PATCH", "latest")):
                version = DatasetVersion(dataset_id=dataset_id)
                db.add(version)
                await db.flush()
                versions.append(version.id)
                db.add(DatasetExampleRevision(dataset_example_id=example_id, dataset_version_id=version.id,
                                              revision_kind=kind, input={"value": value}, output={"answer": "hidden"}))
        async def send(ws, method, params):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": method, "method": method, "params": params}))

        async def recv(ws):
            return json.loads(await asyncio.wait_for(ws.recv(), 4))

        async with self.connection() as ws:
            capabilities = ["http_json", "http_sse"] if stream_output else ["http_json"]
            hello = await self.hello(ws, capabilities=capabilities)
            response = await self.client.post(f"/api/v1/datasets/{dataset_id}/experiments", json={
                "name": "dispatch", "repetitions": 2, "dataset_version_id": versions[0],
                "execution_config": {"runner_id": self.runner_id, "agent_target_id": "agent",
                                     "timeout_seconds": 12, "stream_output": stream_output},
            })
            self.assertEqual(response.status_code, 201, response.text)
            url = response.headers["location"]
            experiment_id = response.json()["experiment_id"]
            self.assertEqual(response.json()["progress"]["queued"], 2)
            await send(ws, "runner.sync", {"runner_id": self.runner_id, "active_jobs": [], "pending_results": []})
            self.assertEqual((await recv(ws))["result"], {"jobs": []})
            self.assertEqual((await self.client.get(url)).json()["progress"]["queued"], 2)
            await send(ws, "runner.ready", {"session_id": hello["session_id"]})
            ready, dispatch = await recv(ws), await recv(ws)
            if dispatch["id"] == "runner.ready":
                ready, dispatch = dispatch, ready
            self.assertEqual(ready["result"]["status"], "ready")

            for repetition in (1, 2):
                self.assertEqual(dispatch["method"], "jobs.dispatch")
                payload = dispatch["params"]
                key = payload["job_id"], payload["attempt_id"]
                self.assertEqual(payload, {
                    "job_id": key[0], "attempt_id": key[1], "agent_target_id": "agent", "example_id": str(example_id),
                    "input": {"value": "frozen"}, "timeout_seconds": 12,
                    **({"stream_output": True} if stream_output else {}),
                })
                async with self.database.transaction() as db:
                    job = await db.scalar(select(Job).where(Job.job_id == key[0]))
                    attempt = await db.scalar(select(RunnerAttempt).where(RunnerAttempt.attempt_id == key[1]))
                    self.assertEqual((job.repetition_number, job.status, attempt.status), (repetition, "assigned", "assigned"))
                    self.assertEqual(attempt.dispatch_json, payload)
                if stream_output:
                    # 首片在 accepted / started / complete 之前到达，仍应有正确的坐标和快照。
                    chunks = [{"seq": 1, "text": "订单"}, {"seq": 2, "text": "已完成 🧪"}]
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "jobs.output_chunk", "params": {
                        "job_id": key[0], "attempt_id": key[1], "chunks": chunks,
                    }}))
                    store = self.app.state.experiment_events
                    target = PreviewTarget(experiment_id, *key)
                    async with asyncio.timeout(2):
                        while (snapshot := await store.read_preview(target)) is None:
                            await asyncio.sleep(0.01)
                    self.assertEqual([chunk.model_dump() for chunk in snapshot.chunks], chunks)
                # 模拟 Agent 先于接单响应完成；迟到的 accepted 不得回退终态。
                await send(ws, "jobs.complete", result_params(key))
                self.assertEqual((await recv(ws))["result"], {"acknowledged": True})
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": dispatch["id"], "result": {"accepted": True}}))
                if repetition == 1:
                    dispatch = await recv(ws)
            result = (await self.client.get(url)).json()
            self.assertEqual((result["status"], result["progress"]["succeeded"]), ("completed", 2))
            async with asyncio.timeout(3):
                while True:
                    async with self.database.transaction() as db:
                        attempt = await db.scalar(select(RunnerAttempt).where(RunnerAttempt.attempt_id == key[1]))
                        self.assertEqual(attempt.status, "succeeded")
                        if attempt.accepted_at is not None:
                            break
                    await asyncio.sleep(0.01)

    async def test_experiment_cancel_reaches_runner_and_survives_reconnect_until_final_result(self):
        owner = await self.user()
        key = await seed_attempt(self.database, owner.id, self.runner_id)
        async with self.database.transaction() as db:
            experiment = await db.scalar(select(Experiment))
            experiment.execution_config = {"runner_id": self.runner_id, "agent_target_id": "agent", "timeout_seconds": 60}
            experiment_id = experiment.id
        path = f"/api/v1/experiments/{experiment_id}"
        snapshot = {"runner_id": self.runner_id, "active_jobs": [{
            "job_id": key[0], "attempt_id": key[1], "status": "running"}], "pending_results": []}

        async def call(ws, method, params):
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "id": method, "params": params}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 4))
            self.assertEqual(reply["id"], method)
            return reply["result"]

        async with self.connection() as ws:
            hello = await self.hello(ws)
            self.assertEqual((await call(ws, "runner.sync", snapshot))["jobs"][0]["action"], "continue")
            await call(ws, "runner.ready", {"session_id": hello["session_id"]})
            response = await self.client.post(path + "/cancel")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "cancelling")
            request = json.loads(await asyncio.wait_for(ws.recv(), 4))
            self.assertEqual(request["method"], "jobs.cancel")
            self.assertEqual(request["params"], {"job_id": key[0], "attempt_id": key[1], "reason": "user_requested"})
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"accepted": True}}))
            self.assertEqual((await self.client.get(path)).json()["status"], "cancelling")

        async with self.connection() as ws:
            await self.hello(ws)
            self.assertEqual((await call(ws, "runner.sync", snapshot))["jobs"][0]["action"], "cancel")
            # 客户端可能已取得结果，取消不能覆盖这次执行的真实终态。
            self.assertEqual(await call(ws, "jobs.complete", result_params(key)), {"acknowledged": True})
        result = (await self.client.get(path)).json()
        self.assertEqual((result["status"], result["progress"]["succeeded"]), ("cancelled", 1))
        self.assertEqual((await self.client.post(path + "/cancel")).json(), result)

    async def test_missing_hello_closes_idle_connection(self):
        self.app.state.settings = replace(self.app.state.settings, runner_hello_timeout_seconds=0.1)
        async with self.connection() as ws:
            with self.assertRaises(ConnectionClosed) as caught:
                await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(caught.exception.rcvd.code, 1008)

    async def test_expired_session_registration_closes_socket_without_resurrecting_it(self):
        async with self.connection() as ws:
            await self.hello(ws)
            key = "evalforge:runner:session:" + self.runner_id
            await self.redis.pexpire(key, 1)
            with self.assertRaises(ConnectionClosed) as caught:
                await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(caught.exception.rcvd.code, 1008)
            self.assertIsNone(await self.redis.get(key))

    async def test_session_storage_outage_fails_closed_with_server_error(self):
        async with self.connection() as ws:
            await self.hello(ws)
            connections = self.app.state.runner_sessions.connections
            with patch.object(connections, "renew", side_effect=RedisConnectionError("private redis URI")):
                with self.assertRaises(ConnectionClosed) as caught:
                    await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(caught.exception.rcvd.code, 1011)

    async def test_manager_shutdown_closes_wss_and_removes_registration(self):
        async with self.connection() as ws:
            await self.hello(ws)
            await asyncio.wait_for(self.app.state.runner_sessions.close(), 2)
            with self.assertRaises(ConnectionClosed) as caught:
                await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(caught.exception.rcvd.code, 1001)
        self.assertFalse(self.app.state.runner_sessions.sessions)
        self.assertIsNone(await self.redis.get("evalforge:runner:session:" + self.runner_id))

    async def test_bad_browser_and_missing_credentials_are_rejected_before_upgrade(self):
        for token in ("bad", self.token[:-1] + ("x" if self.token[-1] != "x" else "y"),
                      self.client.cookies.get(self.config.cookie_name)):
            with self.subTest(token_type="invalid"):
                with self.assertRaises(InvalidStatus) as caught:
                    async with self.connection(token):
                        self.fail("Must reject before upgrade")
                self.assertEqual(caught.exception.response.status_code, 401)
                self.assertNotIn(token.encode(), caught.exception.response.body)
        with self.assertRaises(InvalidStatus) as caught:
            async with connect(self.url, ssl=self.ssl, proxy=None):
                self.fail("Missing credential")
        self.assertEqual(caught.exception.response.status_code, 401)

    async def test_expired_revoked_disabled_and_deleted_identities_are_rejected(self):
        for case, expected in (("expired", 401), ("revoked", 401), ("runner_disabled", 403),
                               ("user_disabled", 403), ("deleted", 401)):
            async with self.database.transaction() as db:
                credential = await db.get(RunnerCredential, self.credential_id)
                runner, user = await db.get(Runner, self.runner_id), await db.get(User, 1)
                credential.created_at = datetime.now(timezone.utc) - timedelta(days=2)
                credential.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
                credential.revoked_at, runner.deleted_at = None, None
                runner.enabled = user.enabled = True
                if case == "expired":
                    credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                elif case == "revoked":
                    credential.revoked_at = datetime.now(timezone.utc)
                elif case == "runner_disabled":
                    runner.enabled = False
                elif case == "user_disabled":
                    user.enabled = False
                else:
                    runner.deleted_at, runner.enabled = datetime.now(timezone.utc), False
                    credential.revoked_at = datetime.now(timezone.utc)
            with self.subTest(case=case), self.assertRaises(InvalidStatus) as caught:
                async with self.connection():
                    self.fail("Invalid identity")
            self.assertEqual(caught.exception.response.status_code, expected)

    async def test_origin_and_url_query_are_checked_without_cookie_fallback(self):
        for options, expected in (({"origin": "https://evil.example"}, 403), ({"origin": "null"}, 403),
                                  ({"suffix": "?token=private-secret"}, 400)):
            with self.subTest(options=options), self.assertRaises(InvalidStatus) as caught:
                async with self.connection(**options):
                    self.fail("Invalid origin / query")
            self.assertEqual(caught.exception.response.status_code, expected)
            self.assertNotIn(b"private-secret", caught.exception.response.body)

    async def test_rpc_parse_errors_batches_notifications_and_internal_error_privacy(self):
        notified = asyncio.Event()

        def setup(peer, identity):
            self.assertEqual(identity.runner_id, self.runner_id)
            self.assertEqual(identity.owner_user_id, 1)
            async def echo(value):
                return value
            async def notice():
                notified.set()
            async def broken():
                raise RuntimeError("private-token-must-not-leak")
            peer.register("test.echo", echo)
            peer.register("test.notice", notice)
            peer.register("test.broken", broken)
        self.app.state.configure_runner_rpc = setup
        async with self.connection() as ws:
            for payload, code in (("{bad", -32700), ("[]", -32600), ("7", -32600),
                                  ('{"jsonrpc":"2.0","method":"test.echo","params":{"wrong":1},"id":3}', -32602),
                                  ('{"jsonrpc":"2.0","method":"test.broken","id":4}', -32603)):
                await ws.send(payload)
                reply = await asyncio.wait_for(ws.recv(), 2)
                self.assertEqual(json.loads(reply)["error"]["code"], code)
                self.assertNotIn("private", reply)
            await ws.send(json.dumps([
                {"jsonrpc": "2.0", "method": "test.echo", "params": ["中文"], "id": "a"},
                {"jsonrpc": "2.0", "method": "test.notice"},
                {"jsonrpc": "2.0", "method": "test.echo", "params": [None], "id": None},
            ]))
            replies = json.loads(await asyncio.wait_for(ws.recv(), 2))
            self.assertEqual([item["id"] for item in replies], ["a", None])
            self.assertEqual(replies[0]["result"], "中文")
            await asyncio.wait_for(notified.wait(), 1)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.05)

    async def test_real_client_transport_and_rpc_can_call_back_over_tls(self):
        def setup(peer, identity):
            async def reverse(value):
                return await peer.call("client.double", [value], timeout=2)
            peer.register("test.reverse", reverse)
        self.app.state.configure_runner_rpc = setup
        root = Path(__file__).resolve().parents[1]
        client_python = root / "client/.venv/Scripts/python.exe"
        if not client_python.exists():
            client_python = Path(sys.executable)
        script = """
import asyncio, json, sys
from evalforge_client.transport import WebSocketTransport
from evalforge_client.rpc import JsonRpcPeer
async def main():
    config = json.loads(sys.stdin.read())
    async with WebSocketTransport(config['url'], bearer_token=config['token']).open() as connection:
        async with JsonRpcPeer(connection) as peer:
            async def double(value): return value * 2
            peer.register('client.double', double)
            assert await peer.call('test.reverse', [21], timeout=3) == 42
    print('CLIENT_RPC_OK')
asyncio.run(main())
"""
        process = await asyncio.create_subprocess_exec(
            str(client_python), "-X", "utf8", "-W", "error", "-c", script,
            cwd=str(root / "client"), env={**os.environ, "SSL_CERT_FILE": str(self.cert)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps({"url": self.url, "token": self.token}).encode()), 10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertIn(b"CLIENT_RPC_OK", stdout)

    async def test_binary_and_oversized_messages_close_connection(self):
        for payload, code in ((b"binary", 1003), ("x" * (1048576 + 1), 1009)):
            async with self.connection() as ws:
                await ws.send(payload)
                with self.assertRaises(ConnectionClosed) as caught:
                    await asyncio.wait_for(ws.recv(), 2)
                self.assertEqual(caught.exception.rcvd.code, code)

    async def test_idle_connections_close_after_deletion_and_browser_logout_does_not_revoke(self):
        async with self.connection() as ws:
            await self.hello(ws)
            logout = await self.client.post("/api/v1/auth/logout-all")
            self.assertEqual(logout.status_code, 204)
            await (await ws.ping())
            await self.login()
            deleted = await self.client.delete(f"/api/v1/runners/{self.runner_id}")
            self.assertEqual(deleted.status_code, 204)
            with self.assertRaises(ConnectionClosed) as caught:
                await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(caught.exception.rcvd.code, 1008)

    async def test_database_outage_is_sanitized_and_existing_connection_fails_closed(self):
        error = OperationalError("private database URI", {}, Exception("private secret"))
        with patch.object(self.database, "transaction", side_effect=error):
            with self.assertRaises(InvalidStatus) as caught:
                async with self.connection():
                    self.fail("Unavailable authentication")
        self.assertEqual(caught.exception.response.status_code, 503)
        self.assertNotIn(b"private", caught.exception.response.body)
        async with self.connection() as ws:
            await self.hello(ws)
            with patch.object(self.database, "transaction", side_effect=error):
                with self.assertRaises(ConnectionClosed) as caught:
                    await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(caught.exception.rcvd.code, 1011)

    async def test_idle_connection_expires_without_waiting_for_an_application_message(self):
        async with self.connection() as ws:
            await self.hello(ws)
            async with self.database.transaction() as db:
                credential = await db.get(RunnerCredential, self.credential_id)
                credential.expires_at = credential.created_at + timedelta(microseconds=1)
            with self.assertRaises(ConnectionClosed) as caught:
                await asyncio.wait_for(ws.recv(), 3)
            self.assertEqual(caught.exception.rcvd.code, 1008)

    async def test_application_messages_do_not_check_credentials_or_session_in_transport(self):
        self.app.state.settings = replace(self.config, runner_session_heartbeat_seconds=30)
        auth = self.app.state.runner_auth
        connections = self.app.state.runner_sessions.connections
        async with self.connection() as ws:
            await self.hello(ws)
            with patch.object(auth, "check", wraps=auth.check) as check, \
                 patch.object(connections, "is_current", wraps=connections.is_current) as is_current:
                for request_id in range(3):
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "test.unknown", "id": request_id}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    self.assertEqual((reply["id"], reply["error"]["code"]), (request_id, -32601))
                check.assert_not_awaited()
                is_current.assert_not_awaited()

    async def test_plain_ws_is_rejected_before_token_validation(self):
        sent = []
        async def send(message):
            sent.append(message)
        async def receive():
            return {"type": "websocket.connect"}
        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/api/v1/runner/ws",
                 "root_path": "", "query_string": b"", "headers": [(b"host", b"localhost")],
                 "server": ("localhost", 80), "client": ("127.0.0.1", 1234), "subprotocols": [],
                 "extensions": {"websocket.http.response": {}}}
        await self.app(scope, receive, send)
        self.assertEqual(sent[0]["type"], "websocket.http.response.start")
        self.assertEqual(sent[0]["status"], 403)
        self.assertFalse(any(item["type"] == "websocket.accept" for item in sent))
