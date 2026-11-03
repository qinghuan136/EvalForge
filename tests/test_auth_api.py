"""ASGI 接口测试：实际 Argon2、异步 SQLite、fakeredis Lua；不替代真实 PG/Redis 联调。"""

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fakeredis.aioredis
import httpx
from argon2.exceptions import HashingError, VerificationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession

from evalforge.db import Database
from evalforge.db.models import Base, Runner, RunnerCredential, User
from evalforge.server.app import create_app
from evalforge.server.config import ServerSettings
from evalforge.server.errors import APIError
from evalforge.server.security import token_digest
from evalforge.server.sessions import PREFIX

ORIGIN = "https://testserver"
PASSWORD = "Correct horse battery staple"
NEW_PASSWORD = "Another correct password 2026"


class SQLiteDatabase(Database):
    """复用生产 Database.transaction，仅替换测试引擎。"""
    def __init__(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")


class AuthAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = SQLiteDatabase()
        self.addAsyncCleanup(self.database.close)
        async with self.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.addAsyncCleanup(self.redis.aclose)
        self.config = ServerSettings(allowed_origins=(ORIGIN,))
        self.app = create_app(self.config, database=self.database, redis_client=self.redis)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.client = self.new_client()
        self.addAsyncCleanup(self.client.aclose)

    def new_client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                 base_url=ORIGIN, headers={"Origin": ORIGIN})

    async def csrf(self, client=None):
        client = client or self.client
        response = await client.get("/api/v1/auth/csrf")
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response

    async def register(self, username="alice"):
        await self.csrf()
        response = await self.client.post("/api/v1/auth/register", json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 201, response.text)
        return response

    async def login(self, client=None, password=PASSWORD):
        client = client or self.client
        await self.csrf(client)
        response = await client.post("/api/v1/auth/login", json={"username": "alice", "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response

    async def user(self):
        async with self.database.transaction() as db:
            return await db.scalar(select(User).where(User.username == "alice"))

    async def test_register_normalizes_username_hashes_password_and_does_not_log_in(self):
        response = await self.register(" ALIce ")
        self.assertEqual(response.json(), {"user": {"id": 1, "username": "alice", "display_name": None}})
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
        user = await self.user()
        self.assertTrue(user.password_hash.startswith("$argon2id$"))
        self.assertNotIn(PASSWORD, user.password_hash)
        self.assertTrue(await self.app.state.auth.passwords.verify(user.password_hash, PASSWORD))

    async def test_login_rotates_session_and_csrf_and_only_stores_token_digest(self):
        await self.register()
        old_token = self.client.cookies.get(self.config.cookie_name)
        old_csrf = self.client.headers["X-CSRF-Token"]
        response = await self.login()
        token = self.client.cookies.get(self.config.cookie_name)
        self.assertNotEqual(token, old_token)
        self.assertNotEqual(response.json()["csrf_token"], old_csrf)
        self.assertIsNone(await self.redis.get(PREFIX + token_digest(old_token)))
        encoded = await self.redis.get(PREFIX + token_digest(token))
        self.assertEqual(json.loads(encoded)["user_id"], 1)
        self.assertNotIn(token, encoded)
        self.assertNotIn(token, response.text)
        cookie = response.headers["set-cookie"]
        for attribute in ("__Host-evalforge_session=", "HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            self.assertIn(attribute, cookie)
        identity = await self.client.get("/api/v1/auth/me")
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(set(identity.json()["user"]), {"id", "username", "display_name"})
        stale = await self.client.get("/api/v1/auth/me", headers={"Cookie": f"{self.config.cookie_name}={old_token}"})
        self.assertEqual(stale.status_code, 401)
        self.assertIsNotNone((await self.user()).last_login_at)

    async def test_missing_wrong_and_cross_origin_csrf_are_rejected(self):
        await self.csrf()
        data = {"username": "alice", "password": PASSWORD}
        good_csrf = self.client.headers.pop("X-CSRF-Token")
        for headers in ({}, {"X-CSRF-Token": "wrong"},
                        {"X-CSRF-Token": good_csrf, "Origin": "https://evil.example"},
                        {"X-CSRF-Token": good_csrf, "Origin": "null"}):
            response = await self.client.post("/api/v1/auth/register", json=data, headers=headers)
            self.assertEqual(response.status_code, 403, response.text)
        missing_origin = await self.client.post("/api/v1/auth/register", json=data,
                                                headers={"X-CSRF-Token": good_csrf, "Origin": ""})
        self.assertEqual(missing_origin.status_code, 403)
        self.assertIsNone(await self.user())

    async def test_referer_fallback_and_disallowed_csrf_bootstrap_origin(self):
        denied = await self.client.get("/api/v1/auth/csrf", headers={"Origin": "https://evil.example"})
        self.assertEqual(denied.status_code, 403)
        await self.csrf()
        del self.client.headers["Origin"]
        allowed = await self.client.post("/api/v1/auth/register", json={"username": "alice", "password": PASSWORD},
                                         headers={"Referer": ORIGIN + "/login"})
        self.assertEqual(allowed.status_code, 201)

    async def test_duplicate_username_and_secret_validation_errors_are_safe(self):
        await self.register()
        duplicate = await self.client.post("/api/v1/auth/register", json={"username": "ALICE", "password": PASSWORD})
        self.assertEqual(duplicate.status_code, 409)
        for data in ({"username": "bob", "password": "private-short"},
                     {"username": "bob", "password": PASSWORD, "enabled": True},
                     {"username": "bob", "password": "private-secret" * 20}):
            response = await self.client.post("/api/v1/auth/register", json=data)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "invalid_request")
            self.assertNotIn("private", response.text)
            self.assertNotIn(PASSWORD, response.text)

    async def test_missing_wrong_and_disabled_accounts_have_same_login_error(self):
        await self.register()
        responses = []
        for name in ("missing", "alice"):
            response = await self.client.post("/api/v1/auth/login", json={"username": name, "password": "wrong"})
            self.assertEqual(response.status_code, 401)
            responses.append(response.json())
        async with self.database.transaction() as db:
            (await db.get(User, 1)).enabled = False
        disabled = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
        self.assertEqual(disabled.status_code, 401)
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(responses[0], disabled.json())

    async def test_logout_removes_only_current_session_and_is_idempotent(self):
        await self.register()
        await self.login()
        sibling = self.new_client()
        self.addAsyncCleanup(sibling.aclose)
        await self.login(sibling)
        token = self.client.cookies.get(self.config.cookie_name)
        result = await self.client.post("/api/v1/auth/logout")
        self.assertEqual(result.status_code, 204)
        self.assertEqual(result.content, b"")
        self.assertIsNone(await self.redis.get(PREFIX + token_digest(token)))
        self.assertIsNone(self.client.cookies.get(self.config.cookie_name))
        self.assertEqual((await self.client.post("/api/v1/auth/logout")).status_code, 204)
        self.assertEqual((await sibling.get("/api/v1/auth/me")).status_code, 200)

    async def test_logout_all_invalidates_replayed_sessions_via_database_version(self):
        await self.register()
        await self.login()
        sibling = self.new_client()
        self.addAsyncCleanup(sibling.aclose)
        await self.login(sibling)
        token = self.client.cookies.get(self.config.cookie_name)
        sibling_token = sibling.cookies.get(self.config.cookie_name)
        response = await self.client.post("/api/v1/auth/logout-all")
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.user()).auth_version, 2)
        for client, raw in ((self.client, token), (sibling, sibling_token)):
            result = await client.get("/api/v1/auth/me", headers={"Cookie": f"{self.config.cookie_name}={raw}"})
            self.assertEqual(result.status_code, 401)

    async def test_password_change_requires_current_password_and_preserves_runner_credentials(self):
        await self.register()
        await self.login()
        sibling = self.new_client()
        self.addAsyncCleanup(sibling.aclose)
        await self.login(sibling)
        async with self.database.transaction() as db:
            runner = Runner(name="machine", owner_user_id=1)
            db.add(RunnerCredential(runner=runner, token_id="runner-public-id", token_hash=b"x" * 32))
        bad = await self.client.post("/api/v1/auth/password",
                                     json={"current_password": "wrong", "new_password": NEW_PASSWORD})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual((await self.user()).auth_version, 1)
        good = await self.client.post("/api/v1/auth/password",
                                      json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(good.status_code, 204)
        self.assertEqual((await sibling.get("/api/v1/auth/me")).status_code, 401)
        await self.csrf()
        old = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
        self.assertEqual(old.status_code, 401)
        await self.login(password=NEW_PASSWORD)
        async with self.database.transaction() as db:
            credential = await db.scalar(select(RunnerCredential))
            self.assertIsNone(credential.revoked_at)
            self.assertTrue(credential.runner_id)

    async def test_disabled_or_deleted_user_cannot_use_existing_session(self):
        await self.register()
        await self.login()
        async with self.database.transaction() as db:
            (await db.get(User, 1)).enabled = False
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
        async with self.database.transaction() as db:
            await db.delete(await db.get(User, 1))
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)

    async def test_absolute_expiry_and_csrf_reads_do_not_extend_session(self):
        await self.register()
        await self.login()
        token = self.client.cookies.get(self.config.cookie_name)
        key = PREFIX + token_digest(token)
        encoded = await self.redis.get(key)
        before = await self.redis.ttl(key)
        await self.csrf()
        self.assertEqual(await self.redis.get(key), encoded)
        self.assertLessEqual(await self.redis.ttl(key), before)
        record = json.loads(encoded)
        record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await self.redis.set(key, json.dumps(record), ex=60)
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)

    async def test_missing_redis_session_and_runner_bearer_cannot_authenticate_browser(self):
        await self.register()
        await self.login()
        await self.redis.flushdb()
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
        self.client.cookies.clear()
        result = await self.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer efr.public.secret"})
        self.assertEqual(result.status_code, 401)

    async def test_login_is_rate_limited_and_counters_have_expiry(self):
        await self.register()
        self.app.state.settings = replace(self.config, login_limit=2)
        for _ in range(2):
            result = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})
            self.assertEqual(result.status_code, 401)
        result = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
        self.assertEqual(result.status_code, 429)
        for key in await self.redis.keys("evalforge:auth:rate:*"):
            self.assertGreater(await self.redis.ttl(key), 0)

    async def test_redis_and_database_errors_fail_closed_without_leaking_secrets(self):
        await self.register()
        await self.login()
        secret = "postgres://private-password@internal/secret"
        with patch.object(self.app.state.sessions.redis, "get", side_effect=RedisConnectionError(secret)):
            response = await self.client.get("/api/v1/auth/me")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(secret, response.text)
        with patch.object(self.app.state.auth.database, "transaction", side_effect=OperationalError(secret, {}, Exception(secret))):
            response = await self.client.get("/api/v1/auth/me")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(secret, response.text)

    async def test_stale_csrf_and_consumed_session_cannot_create_a_second_login(self):
        await self.register()
        old_token = self.client.cookies.get(self.config.cookie_name)
        old_csrf = self.client.headers["X-CSRF-Token"]
        await self.login()
        stale = await self.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": old_csrf})
        self.assertEqual(stale.status_code, 403)
        response = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD},
                                          headers={"Cookie": f"{self.config.cookie_name}={old_token}", "X-CSRF-Token": old_csrf})
        self.assertEqual(response.status_code, 403)

    async def test_cors_only_allows_configured_origin_and_auth_responses_are_not_cached(self):
        headers = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-CSRF-Token,Content-Type"}
        allowed = await self.client.options("/api/v1/auth/login", headers=headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["access-control-allow-origin"], ORIGIN)
        denied = await self.client.options("/api/v1/auth/login", headers={**headers, "Origin": "https://evil.example"})
        self.assertEqual(denied.status_code, 400)
        for path in ("/api/v1/auth/csrf", "/api/v1/auth/me"):
            response = await self.client.get(path)
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_login_commit_failure_does_not_issue_cookie_and_can_recover(self):
        await self.register()
        old_token = self.client.cookies.get(self.config.cookie_name)

        def fail_login_commit(session):
            if any(isinstance(row, User) and row.last_login_at is not None for row in session.dirty):
                raise OperationalError("private database failure", {}, Exception("private failure"))

        event.listen(SyncSession, "before_commit", fail_login_commit)
        try:
            response = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
        finally:
            event.remove(SyncSession, "before_commit", fail_login_commit)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("private", response.text)
        self.assertIsNone(await self.redis.get(PREFIX + token_digest(old_token)))
        self.assertIsNone((await self.user()).last_login_at)
        # 旧 CSRF 会话已消费；重新 bootstrap 后可以正常登录。
        await self.login()
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 200)

    async def test_session_rotation_atomically_consumes_old_session_once(self):
        store = self.app.state.sessions
        token, _ = await store.create_anonymous()
        results = await asyncio.gather(store.rotate(token, 1, 1), store.rotate(token, 1, 1), return_exceptions=True)
        successes = [value for value in results if isinstance(value, tuple)]
        failures = [value for value in results if isinstance(value, APIError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].status, 403)
        self.assertIsNone(await store.load(token))
        self.assertIsNotNone(await store.load(successes[0][0]))

    async def test_login_rechecks_version_after_password_verification(self):
        await self.register()
        passwords = self.app.state.auth.passwords
        original_verify = passwords.verify

        async def verify_then_revoke(encoded, password):
            matched = await original_verify(encoded, password)
            async with self.database.transaction() as db:
                (await db.get(User, 1)).auth_version += 1
            return matched

        with patch.object(passwords, "verify", side_effect=verify_then_revoke):
            response = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)

    async def test_password_resource_failure_is_not_reported_as_wrong_credentials(self):
        await self.register()
        hasher = self.app.state.auth.passwords._hasher
        with patch.object(type(hasher), "verify", side_effect=VerificationError("private resource failure")):
            response = await self.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        with patch.object(type(hasher), "hash", side_effect=HashingError("private memory failure")):
            response = await self.client.post("/api/v1/auth/register", json={"username": "bob", "password": PASSWORD})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)


class ServerConfigurationTests(unittest.TestCase):
    def test_insecure_cookie_is_explicit_and_restricted_to_loopback(self):
        local = ServerSettings(allowed_origins=("http://localhost:8000",), cookie_secure=False)
        self.assertEqual(local.cookie_name, "evalforge_session")
        for origins in (("*",), ("https://cloud.example/path",), ("https://cloud.example:443",),
                        ("https://user:password@cloud.example",), ("null",)):
            with self.subTest(origins=origins), self.assertRaises(ValueError):
                ServerSettings(allowed_origins=origins)
        with self.assertRaises(ValueError):
            ServerSettings(allowed_origins=("http://cloud.example",), cookie_secure=False)
