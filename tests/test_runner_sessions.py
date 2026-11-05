"""Runner 会话行为：注册、单消费者、接管、租约、故障清理。"""

import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fakeredis.aioredis

from evalforge.server.config import ServerSettings
from evalforge.server.errors import APIError
from evalforge.server.rpc import RpcError
from evalforge.server.runner_auth import RunnerIdentity
from evalforge.server.runner_connections import RUNNER_SESSION_PREFIX, RegisteredConnection, RunnerConnections
from evalforge.server.runner_sessions import RunnerSessionManager
from evalforge.server.session_state_machine import SessionState
from evalforge.server.ws_transport import TransportClosed


class Peer:
    async def close(self):
        pass


class RunnerSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.addAsyncCleanup(self.redis.aclose)
        self.connections = RunnerConnections(self.redis)
        self.manager = self.new_manager()
        self.settings = ServerSettings(runner_hello_timeout_seconds=0.3, runner_session_ttl_seconds=1,
                                       runner_session_renew_seconds=0.05, runner_session_operation_timeout_seconds=0.1)
        self.identity = RunnerIdentity("runner-1", 1, 7, datetime.now(timezone.utc) + timedelta(days=1), b"x" * 32)
        self.key = RUNNER_SESSION_PREFIX + self.identity.runner_id

    def new_manager(self):
        manager = RunnerSessionManager(self.connections)
        self.addAsyncCleanup(manager.close)
        return manager

    def session(self, manager=None, **settings):
        return (manager or self.manager).open(self.identity, Peer(), replace(self.settings, **settings))

    def hello(self, session, **changes):
        params = {"runner_id": self.identity.runner_id, "version": "0.1.0", "capabilities": ["http_json"],
                  "max_concurrent_jobs": 1}
        params.update(changes)
        return session.hello(**params)

    async def test_hello_registers_identity_metadata_and_stays_syncing(self):
        session = self.session()
        self.assertIsNone(await self.redis.get(self.key))
        reply = await self.hello(session)
        self.assertEqual(reply, {"session_id": session.session_id, "status": "ready"})
        record = json.loads(await self.redis.get(self.key))
        self.assertEqual(record["phase"], "syncing")
        self.assertEqual(record["owner_user_id"], 1)
        self.assertEqual(record["credential_id"], 7)
        self.assertEqual(record["cloud_instance_id"], self.manager.instance_id)
        self.assertEqual(record["capabilities"], ["http_json"])
        self.assertNotIn("token", record)
        self.assertGreater(await self.redis.pttl(self.key), 0)
        self.assertEqual(session.phase, SessionState.SYNCING)
        with self.assertRaises(RpcError) as caught:
            await self.hello(session)
        self.assertEqual(caught.exception.code, -32001)

    async def test_invalid_hello_does_not_register_or_replace_a_valid_session(self):
        old = self.session()
        await self.hello(old)
        new = self.session()
        for changes in ({"runner_id": "other"}, {"version": " "}, {"version": 1},
                        {"capabilities": ["http_json", "http_json"]}, {"capabilities": [""]},
                        {"max_concurrent_jobs": True}, {"max_concurrent_jobs": 2}, {"extra": "private"}):
            with self.subTest(changes=changes), self.assertRaises(RpcError) as caught:
                await self.hello(new, **changes)
            self.assertEqual(caught.exception.code, -32602)
        self.assertTrue(await self.connections.is_current(self.identity.runner_id, old.session_id))
        self.assertEqual(new.phase, SessionState.CONNECTED)

    async def test_duplicate_hello_is_rejected_before_any_redis_write(self):
        session = self.session()
        await self.hello(session)
        with patch.object(self.connections, "register", wraps=self.connections.register) as register:
            with self.assertRaises(RpcError) as caught:
                await self.hello(session)
            self.assertEqual(caught.exception.code, -32001)
            register.assert_not_called()
        self.assertEqual(session.phase, SessionState.SYNCING)
        self.assertTrue(await self.connections.is_current(self.identity.runner_id, session.session_id))

    async def test_local_phase_waits_for_redis_commit_and_empty_capabilities_remain_an_array(self):
        session = self.session()
        original = self.connections.compare_and_set_phase

        async def commit(runner_id, session_id, expected, record):
            self.assertEqual(session.phase, SessionState.CONNECTED)
            self.assertEqual(expected, "connected")
            self.assertEqual(record.phase, "syncing")
            changed = await original(runner_id, session_id, expected, record)
            self.assertEqual(session.phase, SessionState.CONNECTED)
            return changed

        with patch.object(self.connections, "compare_and_set_phase", side_effect=commit):
            await self.hello(session, capabilities=[])
        record = json.loads(await self.redis.get(self.key))
        self.assertEqual(record["capabilities"], [])
        self.assertEqual(record["owner_user_id"], self.identity.owner_user_id)
        self.assertEqual(session.phase, SessionState.SYNCING)
        with self.assertRaises(AttributeError):
            session.phase = SessionState.READY

    async def test_phase_compare_checks_expected_state_preserves_ttl_and_does_not_resurrect(self):
        current = RegisteredConnection(session_id="phase-test", phase="connected")
        target = RegisteredConnection(session_id="phase-test", phase="syncing")
        await self.connections.register(self.identity.runner_id, current, 1)
        await self.redis.pexpire(self.key, 500)
        self.assertFalse(await self.connections.compare_and_set_phase(
            self.identity.runner_id, current.session_id, "ready", target))
        self.assertEqual(json.loads(await self.redis.get(self.key))["phase"], "connected")
        self.assertTrue(await self.connections.compare_and_set_phase(
            self.identity.runner_id, current.session_id, "connected", target))
        self.assertGreater(await self.redis.pttl(self.key), 0)
        self.assertLessEqual(await self.redis.pttl(self.key), 500)
        with self.assertRaises(ValueError):
            await self.connections.compare_and_set_phase(self.identity.runner_id, "other-session", "syncing", target)
        await self.redis.delete(self.key)
        self.assertFalse(await self.connections.compare_and_set_phase(
            self.identity.runner_id, current.session_id, "connected", target))
        self.assertIsNone(await self.redis.get(self.key))

    async def test_takeover_after_preview_prevents_old_phase_commit(self):
        old, new = self.session(), self.session()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.connections.compare_and_set_phase

        async def blocked(runner_id, session_id, expected, record):
            if session_id == old.session_id:
                entered.set()
                await release.wait()
            return await original(runner_id, session_id, expected, record)

        with patch.object(self.connections, "compare_and_set_phase", side_effect=blocked):
            pending = asyncio.create_task(self.hello(old))
            await asyncio.wait_for(entered.wait(), 0.5)
            try:
                self.assertEqual(old.phase, SessionState.CONNECTED)
                await self.hello(new)
            finally:
                release.set()
            with self.assertRaises(TransportClosed):
                await asyncio.wait_for(pending, 0.5)
        await old.task
        self.assertEqual(old.phase, SessionState.CLOSED)
        self.assertEqual(new.phase, SessionState.SYNCING)
        self.assertTrue(await self.connections.is_current(self.identity.runner_id, new.session_id))

    async def test_lost_phase_commit_response_never_acknowledges_hello(self):
        session = self.session()
        original = self.connections.compare_and_set_phase

        async def response_lost(*args):
            await original(*args)
            raise TimeoutError("private redis URI")

        with patch.object(self.connections, "compare_and_set_phase", side_effect=response_lost):
            with self.assertRaises(TransportClosed):
                await self.hello(session)
            await session.task
        self.assertEqual(session.close_code, 1011)
        self.assertEqual(session.phase, SessionState.CLOSED)
        self.assertIsNone(await self.redis.get(self.key))

    async def test_phase_commit_rejects_changed_redis_phase_even_with_same_session_id(self):
        session = self.session()
        original = self.connections.compare_and_set_phase

        async def changed_phase(runner_id, session_id, expected, record):
            stored = json.loads(await self.redis.get(self.key))
            stored["phase"] = "ready"
            await self.redis.set(self.key, json.dumps(stored), ex=1)
            return await original(runner_id, session_id, expected, record)

        with patch.object(self.connections, "compare_and_set_phase", side_effect=changed_phase):
            with self.assertRaises(TransportClosed):
                await self.hello(session)
            await session.task
        self.assertEqual(session.close_code, 1008)
        self.assertEqual(session.phase, SessionState.CLOSED)

    async def test_stop_after_phase_write_does_not_acknowledge_hello(self):
        session = self.session()
        original = self.connections.compare_and_set_phase

        async def stop_after_write(*args):
            changed = await original(*args)
            session.request_close(1001)
            return changed

        with patch.object(self.connections, "compare_and_set_phase", side_effect=stop_after_write):
            with self.assertRaises(TransportClosed):
                await self.hello(session)
            await session.task
        self.assertEqual(session.close_code, 1001)
        self.assertEqual(session.phase, SessionState.CLOSED)
        self.assertIsNone(await self.redis.get(self.key))

    async def test_local_takeover_stops_only_old_session_and_preserves_new_registration(self):
        old = self.session()
        await self.hello(old)
        new = self.session()
        await self.hello(new)
        await asyncio.wait_for(old.task, 0.5)
        self.assertEqual(old.close_code, 1008)
        self.assertEqual(old.phase, SessionState.CLOSED)
        self.assertNotIn(old.session_id, self.manager.sessions)
        self.assertTrue(await self.connections.is_current(self.identity.runner_id, new.session_id))
        self.assertFalse(await self.connections.renew(self.identity.runner_id, old.session_id, 1))
        self.assertFalse(await self.connections.compare_delete(self.identity.runner_id, old.session_id))

    async def test_cross_instance_takeover_is_detected_by_idle_renewal(self):
        old = self.session()
        await self.hello(old)
        other_manager = self.new_manager()
        new = self.session(other_manager)
        await self.hello(new)
        await asyncio.wait_for(old.task, 0.5)
        self.assertEqual(old.close_code, 1008)
        self.assertTrue(await self.connections.is_current(self.identity.runner_id, new.session_id))
        await self.manager.close()
        self.assertTrue(await self.connections.is_current(self.identity.runner_id, new.session_id))

    async def test_delayed_registration_reply_cannot_stop_the_latest_of_three_connections(self):
        first, second, third = self.session(), self.session(), self.session()
        written, release = asyncio.Event(), asyncio.Event()
        original = self.connections.register

        async def delayed_reply(runner_id, record, ttl):
            previous = await original(runner_id, record, ttl)
            if record.session_id == first.session_id:
                written.set()
                await release.wait()
            return previous

        with patch.object(self.connections, "register", side_effect=delayed_reply):
            pending = asyncio.create_task(self.hello(first))
            await asyncio.wait_for(written.wait(), 0.5)
            try:
                await self.hello(second)
                await self.hello(third)
            finally:
                release.set()
            with self.assertRaises(TransportClosed):
                await asyncio.wait_for(pending, 0.5)
        await asyncio.wait_for(asyncio.gather(first.task, second.task), 0.5)
        await third.check_current()
        self.assertEqual(third.phase, SessionState.SYNCING)
        self.assertTrue(await self.connections.is_current(self.identity.runner_id, third.session_id))

    async def test_idle_lease_is_renewed_beyond_initial_ttl_and_normal_close_removes_it(self):
        session = self.session()
        await self.hello(session)
        await asyncio.sleep(1.1)
        await session.check_current()
        self.assertGreater(await self.redis.pttl(self.key), 0)
        await session.close()
        self.assertEqual(session.phase, SessionState.CLOSED)
        self.assertIsNone(await self.redis.get(self.key))

    async def test_expired_lease_is_not_recreated(self):
        session = self.session()
        await self.hello(session)
        await self.redis.pexpire(self.key, 1)
        await asyncio.sleep(0.02)
        self.assertFalse(await self.connections.renew(self.identity.runner_id, session.session_id, 1))
        await asyncio.wait_for(session.task, 0.5)
        self.assertEqual(session.close_code, 1008)
        self.assertIsNone(await self.redis.get(self.key))

    async def test_missing_hello_times_out_without_creating_a_registration(self):
        session = self.session(runner_hello_timeout_seconds=0.03)
        await asyncio.wait_for(session.task, 0.5)
        self.assertEqual(session.close_code, 1008)
        self.assertIsNone(await self.redis.get(self.key))

    async def test_hello_deadline_also_bounds_an_inflight_registration(self):
        session = self.session(runner_hello_timeout_seconds=0.03)
        original = self.connections.register

        async def blocked_reply(*args):
            await original(*args)
            await asyncio.Event().wait()

        with patch.object(self.connections, "register", side_effect=blocked_reply):
            with self.assertRaises(TransportClosed):
                await asyncio.wait_for(self.hello(session), 0.5)
            await session.task
        self.assertEqual(session.close_code, 1008)
        self.assertIsNone(await self.redis.get(self.key))

    async def test_registration_response_loss_cleans_the_possible_write(self):
        original = self.connections.register

        async def response_lost(*args):
            await original(*args)
            raise TimeoutError("private redis endpoint")

        session = self.session()
        with patch.object(self.connections, "register", side_effect=response_lost):
            with self.assertRaises(TransportClosed):
                await self.hello(session)
            await asyncio.wait_for(session.task, 0.5)
        self.assertEqual(session.close_code, 1011)
        self.assertIsNone(await self.redis.get(self.key))
        self.assertFalse(self.manager.sessions)

    async def test_renewal_storage_failure_closes_and_cleans_session(self):
        session = self.session()
        await self.hello(session)
        with patch.object(self.connections, "renew", side_effect=TimeoutError("private secret")):
            await asyncio.wait_for(session.task, 0.5)
        self.assertEqual(session.close_code, 1011)
        self.assertIsNone(await self.redis.get(self.key))

    async def test_corrupt_or_nonexpiring_registration_is_not_a_valid_lease(self):
        session = self.session()
        await self.hello(session)
        await self.redis.persist(self.key)
        with self.assertRaises(APIError) as caught:
            await session.check_current()
        self.assertEqual(caught.exception.status, 403)
        await self.redis.set(self.key, "not-json", ex=1)
        with self.assertRaises(APIError) as caught:
            await session.check_current()
        self.assertEqual(caught.exception.status, 503)

    async def test_queue_is_bounded_and_shutdown_resolves_inflight_and_queued_waiters(self):
        session = self.session(runner_session_queue_size=1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.connections.register

        async def blocked(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        with patch.object(self.connections, "register", side_effect=blocked):
            first = asyncio.create_task(self.hello(session))
            await asyncio.wait_for(entered.wait(), 0.5)
            second = asyncio.create_task(self.hello(session))
            await asyncio.sleep(0)
            try:
                with self.assertRaises(RpcError) as caught:
                    await self.hello(session)
                self.assertEqual(caught.exception.code, -32000)
            finally:
                session.request_close(1001)
                release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 0.5)
        self.assertTrue(all(isinstance(value, TransportClosed) for value in outcomes), outcomes)
        await session.task
        self.assertIsNone(await self.redis.get(self.key))

    async def test_manager_shutdown_closes_all_sessions_and_rejects_new_connections(self):
        session = self.session()
        await self.hello(session)
        await self.manager.close()
        self.assertFalse(self.manager.sessions)
        self.assertEqual(session.close_code, 1001)
        self.assertIsNone(await self.redis.get(self.key))
        with self.assertRaises(TransportClosed):
            self.session()


class RunnerSessionConfigTests(unittest.TestCase):
    def test_timeouts_capacity_and_renewal_margin_are_validated(self):
        for changes in ({"runner_hello_timeout_seconds": 0}, {"runner_session_queue_size": True},
                        {"runner_session_ttl_seconds": 1.5}, {"runner_session_renew_seconds": float("nan")},
                        {"runner_session_operation_timeout_seconds": float("inf")},
                        {"runner_session_ttl_seconds": 25}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ServerSettings(**changes)

    def test_session_settings_are_read_from_environment(self):
        with patch.dict(os.environ, {"EVALFORGE_RUNNER_HELLO_TIMEOUT_SECONDS": "2",
                                     "EVALFORGE_RUNNER_SESSION_TTL_SECONDS": "30",
                                     "EVALFORGE_RUNNER_SESSION_RENEW_SECONDS": "5",
                                     "EVALFORGE_RUNNER_SESSION_OPERATION_TIMEOUT_SECONDS": "1",
                                     "EVALFORGE_RUNNER_SESSION_QUEUE_SIZE": "4"}, clear=True):
            settings = ServerSettings.from_env("nonexistent-runner-session-test.env")
        self.assertEqual(settings.runner_hello_timeout_seconds, 2)
        self.assertEqual(settings.runner_session_ttl_seconds, 30)
        self.assertEqual(settings.runner_session_renew_seconds, 5)
        self.assertEqual(settings.runner_session_operation_timeout_seconds, 1)
        self.assertEqual(settings.runner_session_queue_size, 4)
