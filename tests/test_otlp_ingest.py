import gzip
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest, ExportTraceServiceResponse,
)
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from api_support import APITestCase
from evalforge.db.models import Runner, RunnerCredential, Span, Trace, User
from evalforge.server.otlp import MAX_BODY_BYTES, PROTOBUF
from evalforge.server.runner_auth import RUNNER_AUTH_CACHE_PREFIX

TRACE_ID = bytes.fromhex("33" * 16)
ROOT_ID = bytes.fromhex("11" * 8)
CHILD_ID = bytes.fromhex("22" * 8)
USE_ALICE_TOKEN = object()


def new_batch():
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add(schema_url="https://example.test/resource")
    resource.resource.attributes.add(key="service.name").value.string_value = "example-agent"
    scope = resource.scope_spans.add(schema_url="https://example.test/scope")
    scope.scope.name = "example-instrumentation"
    return request, scope.spans


class TraceIngestTests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.database.transaction() as db:
            alice = User(username="alice", password_hash="test")
            bob = User(username="bob", password_hash="test")
            db.add_all([alice, bob])
            await db.flush()
            self.alice_id, self.bob_id = alice.id, bob.id
        self.alice_runner = await self.app.state.runners.create(self.alice_id, 1, "Alice Runner")
        self.bob_runner = await self.app.state.runners.create(self.bob_id, 1, "Bob Runner")

    async def send(self, request=None, *, token=USE_ALICE_TOKEN, body=None, **headers):
        if body is None:
            body = request.SerializeToString()
        headers = {"content-type": PROTOBUF, **headers}
        if token is USE_ALICE_TOKEN:
            token = self.alice_runner.credential.token
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        return await self.client.post("/v1/traces", content=body, headers=headers)

    def assert_otlp_success(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], PROTOBUF)
        self.assertFalse(ExportTraceServiceResponse.FromString(response.content).HasField("partial_success"))

    async def test_parent_arrives_late_and_retransmit_keeps_original_span_and_bounds(self):
        request, spans = new_batch()
        child = spans.add(
            trace_id=TRACE_ID, span_id=CHILD_ID, parent_span_id=ROOT_ID,
            name="retrieve", kind=3, start_time_unix_nano=100, end_time_unix_nano=200,
        )
        child.status.code = 2
        child.status.message = "upstream error"
        child.attributes.add(key="gen_ai.request.model").value.string_value = "example"
        child.attributes.add(key="custom.int").value.int_value = 9_007_199_254_740_993
        child.attributes.add(key="custom.bytes").value.bytes_value = b"\x00\x01"
        child.events.add(name="finished", time_unix_nano=198)
        child.links.add(trace_id=TRACE_ID, span_id=ROOT_ID)
        self.assert_otlp_success(await self.send(body=gzip.compress(request.SerializeToString()),
                                                 **{"content-encoding": "gzip"}))

        retry, spans = new_batch()
        spans.add(trace_id=TRACE_ID, span_id=CHILD_ID, name="changed retry", end_time_unix_nano=999)
        spans.add(trace_id=TRACE_ID, span_id=ROOT_ID, name="request",
                  start_time_unix_nano=50, end_time_unix_nano=300)
        self.assert_otlp_success(await self.send(retry))
        self.assert_otlp_success(await self.send(retry))

        async with self.database.transaction() as db:
            trace = await db.scalar(select(Trace))
            self.assertEqual((trace.owner_user_id, trace.trace_id), (self.alice_id, TRACE_ID.hex()))
            self.assertEqual((trace.start_time_unix_nano, trace.end_time_unix_nano), (50, 300))
            rows = (await db.scalars(select(Span).order_by(Span.span_id))).all()
            self.assertEqual([s.name for s in rows], ["request", "retrieve"])
            self.assertIsNone(rows[0].parent_span_id)
            self.assertIsNone(rows[0].status_code)
            saved = rows[1]
            self.assertEqual((saved.parent_span_id, saved.kind, saved.status_code, saved.status_message),
                             (ROOT_ID.hex(), 3, 2, "upstream error"))
            self.assertEqual(saved.attributes, [
                {"key": "gen_ai.request.model", "value": {"stringValue": "example"}},
                {"key": "custom.int", "value": {"intValue": "9007199254740993"}},
                {"key": "custom.bytes", "value": {"bytesValue": "AAE="}},
            ])
            self.assertEqual(saved.events, [{"name": "finished", "timeUnixNano": "198"}])
            self.assertEqual(saved.links, [{"traceId": TRACE_ID.hex(), "spanId": ROOT_ID.hex()}])
            self.assertEqual(saved.resource["schemaUrl"], "https://example.test/resource")
            self.assertEqual(saved.instrumentation_scope["scope"]["name"], "example-instrumentation")

    async def test_upload_token_sets_owner_and_batch_can_span_multiple_statements(self):
        request, spans = new_batch()
        for i in range(1, 108):
            span = spans.add(trace_id=i.to_bytes(16, "big"), span_id=ROOT_ID, name="root",
                             start_time_unix_nano=100, end_time_unix_nano=200)
            span.attributes.add(key="owner_user_id").value.int_value = self.bob_id
        self.assert_otlp_success(await self.send(request))
        self.assert_otlp_success(await self.send(request, token=self.bob_runner.credential.token))
        async with self.database.transaction() as db:
            counts = await db.execute(select(Trace.owner_user_id, func.count()).group_by(Trace.owner_user_id))
            self.assertEqual({owner: count for owner, count in counts}, {self.alice_id: 107, self.bob_id: 107})
            self.assertEqual(await db.scalar(select(func.count()).select_from(Span)), 214)

        async with self.database.transaction() as db:
            (await db.get(User, self.bob_id)).enabled = False
        self.assertEqual((await self.send(request, token=self.bob_runner.credential.token)).status_code, 403)

    async def test_offline_runner_upload_does_not_record_websocket_activity(self):
        request, spans = new_batch()
        spans.add(trace_id=TRACE_ID, span_id=ROOT_ID, name="root", end_time_unix_nano=100)
        runner_id = self.alice_runner.runner.runner_id
        credential_id = self.alice_runner.credential.id
        self.assert_otlp_success(await self.send(request))
        async with self.database.transaction() as db:
            self.assertIsNone((await db.get(Runner, runner_id)).last_seen_at)
            self.assertIsNone((await db.get(RunnerCredential, credential_id)).last_used_at)
        self.assertIsNone(await self.redis.get(RUNNER_AUTH_CACHE_PREFIX + runner_id))

    async def test_disabled_runner_and_revoked_or_expired_credentials_are_rejected(self):
        runner_id = self.alice_runner.runner.runner_id
        credential_id = self.alice_runner.credential.id
        now = datetime.now(timezone.utc)
        async with self.database.transaction() as db:
            (await db.get(Runner, runner_id)).enabled = False
        self.assertEqual((await self.send(body=b"")).status_code, 403)
        async with self.database.transaction() as db:
            (await db.get(Runner, runner_id)).enabled = True
            (await db.get(RunnerCredential, credential_id)).revoked_at = now
        self.assertEqual((await self.send(body=b"")).status_code, 401)
        async with self.database.transaction() as db:
            credential = await db.get(RunnerCredential, credential_id)
            credential.revoked_at = None
            credential.created_at = now - timedelta(days=2)
            credential.expires_at = now - timedelta(days=1)
        self.assertEqual((await self.send(body=b"")).status_code, 401)

    async def test_bad_payload_returns_protobuf_status_without_writing_any_spans(self):
        invalid, spans = new_batch()
        spans.add(trace_id=TRACE_ID, span_id=ROOT_ID, name="root", end_time_unix_nano=100)
        spans.add(trace_id=b"bad", span_id=CHILD_ID, name="bad", end_time_unix_nano=100)
        self.assert_otlp_success(await self.send(body=b""))
        responses = (
            (401, await self.send(body=b"", token=None)),
            (401, await self.send(body=b"", token="a" * 43)),
            (415, await self.send(body=b"{}", **{"content-type": "application/json"})),
            (400, await self.send(body=b"\xff")),
            (400, await self.send(invalid)),
            (400, await self.send(body=b"not gzip", **{"content-encoding": "gzip"})),
            (413, await self.send(body=b"x" * (MAX_BODY_BYTES + 1))),
            (413, await self.send(body=gzip.compress(b"x" * (MAX_BODY_BYTES + 1)),
                                  **{"content-encoding": "gzip"})),
        )
        for expected, response in responses:
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.headers["content-type"], PROTOBUF)
            self.assertTrue(Status.FromString(response.content).message)
        with patch("evalforge.server.otlp.MAX_SPANS", 0):
            self.assertEqual((await self.send(invalid)).status_code, 413)
        async with self.database.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Trace)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Span)), 0)

    async def test_failed_commit_path_rolls_back_batch_and_is_retryable(self):
        request, spans = new_batch()
        spans.add(trace_id=TRACE_ID, span_id=ROOT_ID, name="root", end_time_unix_nano=100)

        def fail_span_write(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO spans"):
                raise OperationalError(None, None, RuntimeError("db unavailable"))

        engine = self.database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", fail_span_write)
        try:
            response = await self.send(request)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(Status.FromString(response.content).message, "Trace service unavailable")
            async with self.database.transaction() as db:
                self.assertEqual(await db.scalar(select(func.count()).select_from(Trace)), 0)
        finally:
            event.remove(engine, "before_cursor_execute", fail_span_write)
        self.assert_otlp_success(await self.send(request))
