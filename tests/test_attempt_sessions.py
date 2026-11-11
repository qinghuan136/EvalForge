import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from dataclasses import replace

import fakeredis.aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from attempt_support import result_params
from evalforge.server.config import ServerSettings
from evalforge.server.experiment_events import PreviewTarget
from evalforge.server.rpc import RpcError
from evalforge.server.runner_auth import RunnerIdentity
from evalforge.server.runner_connections import RUNNER_SESSION_PREFIX, RunnerConnections
from evalforge.server.runner_sessions import RunnerSessionManager
from evalforge.server.session_state_machine import SessionState
from evalforge.server.ws_transport import TransportClosed


class Peer:
    async def close(self):
        pass


class AttemptSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.addAsyncCleanup(self.redis.aclose)
        self.service = AsyncMock()
        self.service.sync.return_value = {"jobs": []}
        self.manager = RunnerSessionManager(RunnerConnections(self.redis), self.service, runner_auth=AsyncMock())
        self.addAsyncCleanup(self.manager.close)
        self.identity = RunnerIdentity("runner", 1, 1, datetime.now(timezone.utc) + timedelta(days=1), b"x" * 32)
        self.session = self.manager.open(self.identity, Peer(), ServerSettings())
        self.result = result_params(("job", "attempt"))

    async def hello(self, session=None, *, capabilities=None):
        session = session or self.session
        return await session.hello(runner_id="runner", version="0.1", capabilities=capabilities or [],
                                   max_concurrent_jobs=1)

    async def test_preview_only_accepts_its_attempt_and_slow_redis_does_not_block_completion(self):
        store = self.manager.event_store = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_append(*args):
            entered.set()
            await release.wait()
            raise RedisConnectionError("private address")

        store.append_preview.side_effect = slow_append
        target = PreviewTarget(42, "job", "attempt")
        chunks = [{"seq": 1, "text": "first"}]
        await self.hello(capabilities=["http_json", "http_sse"])
        await self.sync()
        await self.session.ready(session_id=self.session.session_id)
        self.session.set_preview_target(target)
        await self.session.output_chunk(job_id="job", attempt_id="another", chunks=chunks)
        store.append_preview.assert_not_awaited()

        with patch.object(self.manager.connections, "is_current", wraps=self.manager.connections.is_current) as check:
            task = asyncio.create_task(self.session.output_chunk(job_id="job", attempt_id="attempt", chunks=chunks))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await self.session.output_chunk(job_id="job", attempt_id="attempt",
                                                chunks=[{"seq": 2, "text": "during write"}])
                check.assert_not_awaited()
                self.assertEqual(await asyncio.wait_for(self.session.complete(**self.result), 1),
                                 {"acknowledged": True})
            finally:
                release.set()
                await task
        await self.session.output_chunk(job_id="job", attempt_id="attempt", chunks=chunks)
        store.append_preview.assert_awaited_once()

    async def sync(self, pending=False, active=False):
        return await self.session.sync(runner_id="runner", active_jobs=[{
            "job_id": "job", "attempt_id": "attempt", "status": "running"}] if active else [],
            pending_results=[self.result] if pending else [])

    async def test_ready_requires_sync_and_historical_results_committed(self):
        await self.hello()
        with self.assertRaises(RpcError):
            await self.session.ready(session_id=self.session.session_id)
        await self.sync(pending=True)
        with self.assertRaises(RpcError):
            await self.session.ready(session_id=self.session.session_id)
        await self.session.complete(**self.result)
        reply = await self.session.ready(session_id=self.session.session_id)
        self.assertEqual(reply["status"], "ready")
        self.assertEqual(self.session.phase, SessionState.READY)
        record = json.loads(await self.redis.get(RUNNER_SESSION_PREFIX + "runner"))
        self.assertEqual(record["phase"], "ready")
        self.assertEqual(await self.session.ready(session_id=self.session.session_id), reply)

    async def test_active_job_does_not_block_ready_and_can_finish_afterwards(self):
        await self.hello()
        await self.sync(active=True)
        await self.session.ready(session_id=self.session.session_id)
        await self.session.started(job_id="job", attempt_id="attempt")
        self.assertEqual(await self.session.complete(**self.result), {"acknowledged": True})

    async def test_completion_during_syncing_after_snapshot_is_accepted(self):
        await self.hello()
        await self.sync(active=True)
        await self.session.complete(**self.result)
        self.assertFalse(self.session.pending_uploads)
        await self.session.ready(session_id=self.session.session_id)

    async def test_database_failure_returns_error_once_keeps_pending_and_session_alive(self):
        await self.hello()
        await self.sync(pending=True)
        self.service.complete.side_effect = OperationalError("secret SQL", {}, Exception("private output"))
        with self.assertLogs("evalforge.server.runner_sessions", level="ERROR") as logs:
            with self.assertRaises(RpcError) as caught:
                await self.session.complete(**self.result)
        self.assertEqual(caught.exception.code, -32003)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private", str(logs.output))
        self.assertNotIn("secret", str(logs.output))
        self.service.complete.assert_awaited_once()
        self.assertEqual(self.session.pending_uploads, {("job", "attempt")})
        self.assertFalse(self.session.task.done())
        self.service.complete.side_effect = None
        await self.session.complete(**self.result)
        await self.session.ready(session_id=self.session.session_id)

    async def test_sync_failure_does_not_confirm_sync_or_allow_ready(self):
        await self.hello()
        self.service.sync.side_effect = OperationalError("secret", {}, Exception())
        with self.assertLogs("evalforge.server.runner_sessions", level="ERROR"):
            with self.assertRaises(RpcError):
                await self.sync()
        self.assertFalse(self.session.sync_processed)
        self.assertEqual(self.session.phase, SessionState.SYNCING)

    async def test_invalid_phase_snapshot_and_ready_identity(self):
        with self.assertRaises(RpcError):
            await self.sync()
        await self.hello()
        with self.assertRaises(RpcError):
            await self.session.complete(**self.result)
        with self.assertRaises(RpcError):
            await self.session.sync(runner_id="runner", active_jobs=[], pending_results=[self.result, self.result])
        await self.sync()
        with self.assertRaises(RpcError):
            await self.sync()
        with self.assertRaises(RpcError):
            await self.session.ready(session_id="old-session")
        self.assertEqual(self.session.phase, SessionState.SYNCING)

    async def test_pending_uploads_are_removed_only_after_service_returns(self):
        await self.hello()
        await self.sync(pending=True)
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
        self.service.complete.side_effect = delayed
        upload = asyncio.create_task(self.session.complete(**self.result))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(self.session.pending_uploads, {("job", "attempt")})
            release.set()
            await upload
            self.assertFalse(self.session.pending_uploads)
        finally:
            release.set()
            await asyncio.gather(upload, return_exceptions=True)

    async def test_takeover_during_save_does_not_ack_old_session_or_change_new_phase(self):
        await self.hello()
        await self.sync(pending=True)
        new = self.manager.open(self.identity, Peer(), ServerSettings())
        async def replace(*args):
            await self.hello(new)
        self.service.complete.side_effect = replace
        with self.assertRaises(TransportClosed):
            await self.session.complete(**self.result)
        self.assertEqual(new.phase, SessionState.SYNCING)
        record = json.loads(await self.redis.get(RUNNER_SESSION_PREFIX + "runner"))
        self.assertEqual(record["session_id"], new.session_id)

    async def test_heartbeat_checks_and_renews_while_business_handler_is_waiting(self):
        self.session.settings = replace(self.session.settings, runner_session_ttl_seconds=3,
                                        runner_session_heartbeat_seconds=0.05,
                                        runner_session_operation_timeout_seconds=1)
        await self.hello()
        await self.sync()
        await self.session.ready(session_id=self.session.session_id)
        entered, release, renewed_during_handler = asyncio.Event(), asyncio.Event(), asyncio.Event()
        renew = self.manager.connections.renew

        async def started(*args):
            entered.set()
            await release.wait()

        async def observe_renew(*args):
            result = await renew(*args)
            if entered.is_set() and result:
                renewed_during_handler.set()
            return result

        self.service.started.side_effect = started
        with patch.object(self.manager.connections, "renew", side_effect=observe_renew):
            task = asyncio.create_task(self.session.started(job_id="job", attempt_id="attempt"))
            try:
                await asyncio.wait_for(renewed_during_handler.wait(), 0.8)
                self.manager.runner_auth.check.assert_awaited_with(self.identity)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_unexpected_business_error_is_logged_once_without_killing_session(self):
        await self.hello()
        await self.sync()
        self.service.complete.side_effect = RuntimeError("private output")
        with self.assertLogs("evalforge.server.runner_sessions", level="ERROR") as logs:
            with self.assertRaises(RpcError) as caught:
                await self.session.complete(**self.result)
        self.assertEqual(caught.exception.code, -32603)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private", str(logs.output))
        self.assertFalse(self.session.task.done())

    async def test_ready_redis_commit_response_lost_closes_session_without_ack(self):
        await self.hello()
        await self.sync()
        original = self.manager.connections.compare_and_set_phase
        async def lost(*args):
            await original(*args)
            raise TimeoutError()
        with patch.object(self.manager.connections, "compare_and_set_phase", side_effect=lost):
            with self.assertLogs("evalforge.server.runner_sessions", level="WARNING"):
                with self.assertRaises(TransportClosed):
                    await self.session.ready(session_id=self.session.session_id)
        await self.session.task
        self.assertEqual(self.session.phase, SessionState.CLOSED)
        self.assertEqual(self.session.close_code, 1011)
        self.assertIsNone(await self.redis.get(RUNNER_SESSION_PREFIX + "runner"))
