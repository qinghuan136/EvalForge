"""Redis 配置、资源与失败语义；回环 RESP 测试端不是实际 Redis 实例。"""

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

from evalforge.redis import RedisSettings, RedisStore
from evalforge.redis.config import connection_options


class RedisConfigurationTests(unittest.TestCase):
    def test_urls_credentials_and_tls_are_validated_without_exposing_values(self):
        settings = RedisSettings("rediss://alice:p%40ss%25word@[::1]:6380/2")
        options = connection_options(settings.url)
        self.assertEqual(options, {"host": "::1", "port": 6380, "db": 2,
                                   "username": "alice", "password": "p@ss%word", "ssl": True})
        self.assertNotIn("alice", repr(settings))
        self.assertNotIn("word", repr(settings))
        self.assertEqual(connection_options("redis://localhost")["db"], 0)
        for url in (None, "", "http://localhost", "redis:///0", "redis://localhost:0", "redis://localhost:70000",
                    "redis://localhost/-1", "redis://localhost/not-a-db", "redis://localhost/0/1",
                    "redis://localhost?ssl_cert_reqs=none", "redis://localhost#secret", "redis://user@localhost",
                    "redis://:bad%FF@localhost", "redis://:bad%ZZ@localhost", " redis://localhost", "redis://local\nhost"):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                RedisSettings(url)
            self.assertNotIn("bad%", str(caught.exception))

    def test_env_overrides_file_without_mutating_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("EVALFORGE_REDIS_URL=redis://file-host/1\nEVALFORGE_REDIS_MAX_CONNECTIONS=3\n", encoding="utf-8")
            with patch.dict(os.environ, {"EVALFORGE_REDIS_URL": "redis://env-host/2"}, clear=True):
                before = dict(os.environ)
                settings = RedisSettings.from_env(path)
                self.assertEqual(connection_options(settings.url)["host"], "env-host")
                self.assertEqual(settings.max_connections, 3)
                self.assertEqual(dict(os.environ), before)
                os.environ["EVALFORGE_REDIS_SOCKET_TIMEOUT"] = "password-secret"
                with self.assertRaises(ValueError) as caught:
                    RedisSettings.from_env(path)
                self.assertNotIn("password-secret", str(caught.exception))
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                RedisSettings.from_env(Path(directory) / "missing.env")

    def test_invalid_timeouts_and_pool_sizes_are_rejected(self):
        for options in ({"connect_timeout": 0}, {"socket_timeout": float("inf")}, {"socket_timeout": True},
                        {"health_check_interval": -1}, {"max_connections": 0}, {"max_connections": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                RedisSettings("redis://localhost", **options)
        self.assertEqual(RedisSettings("redis://localhost", health_check_interval=0).health_check_interval, 0)

    def test_cli_config_check_does_not_connect_or_print_connection_secrets(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("EVALFORGE_REDIS_")}
        env["EVALFORGE_REDIS_URL"] = "redis://username-secret:password-secret@not-contacted.invalid/0"
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "evalforge.redis", "check-config",
                                 "--env-file", ".env.example"], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no connection was attempted", result.stdout)
        self.assertNotIn("secret", result.stdout + result.stderr)


class RedisResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.commands, self.handlers = [], set()
        self.closed, self.blocked = asyncio.Event(), asyncio.Event()
        self.counter = 0
        self.ping_error = False
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = f"redis://alice:p%40ss@127.0.0.1:{self.server.sockets[0].getsockname()[1]}/2"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        handlers = list(self.handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            while line := await reader.readline():
                parts = []
                for _ in range(int(line[1:])):
                    length = int((await reader.readline())[1:])
                    parts.append((await reader.readexactly(length + 2))[:-2].decode("utf-8"))
                self.commands.append(parts)
                command = parts[0].upper()
                if command in ("INCR", "INCRBY"):
                    self.counter += 1  # 已执行写入，但故意丢失响应。
                    return
                if command == "BLOCK":
                    self.blocked.set()
                    await reader.read()
                    return
                reply = b"+PONG\r\n" if command == "PING" else b"+OK\r\n"
                if command == "PING" and self.ping_error:
                    reply = b"-ERR server-secret\r\n"
                writer.write(reply)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.closed.set()
            self.handlers.discard(task)

    async def test_lazy_client_auth_db_and_pool_are_closed_at_context_exit(self):
        store = RedisStore(RedisSettings(self.url, max_connections=2))
        with self.assertRaises(RuntimeError):
            _ = store.client
        async with store:
            self.assertEqual(self.commands, [])
            with self.assertRaises(RuntimeError):
                await store.__aenter__()
            self.assertTrue(await store.check_connection())
            self.assertEqual(store.client.connection_pool.max_connections, 2)
        await asyncio.wait_for(self.closed.wait(), 3)
        self.assertIn(["AUTH", "alice", "p@ss"], self.commands)
        self.assertIn(["SELECT", "2"], self.commands)
        with self.assertRaises(RuntimeError):
            _ = store.client
        await store.close()
        async with RedisStore(RedisSettings("rediss://localhost")) as tls_store:
            options = tls_store.client.connection_pool.connection_kwargs
            self.assertEqual(options["ssl_cert_reqs"], "required")
            self.assertTrue(options["ssl_check_hostname"])

    async def test_applied_write_with_lost_response_is_not_automatically_retried(self):
        async with RedisStore(RedisSettings(self.url)) as store:
            with self.assertRaises(RedisConnectionError):
                await store.client.incr("counter")
        self.assertEqual(self.counter, 1)
        self.assertEqual(sum(command[0] == "INCRBY" or command[0] == "INCR" for command in self.commands), 1)

    async def test_socket_timeout_surfaces_and_closes_connection(self):
        async with RedisStore(RedisSettings(self.url, socket_timeout=0.1)) as store:
            with self.assertRaises(RedisTimeoutError):
                await store.client.execute_command("BLOCK")
        await asyncio.wait_for(self.closed.wait(), 3)
        self.assertEqual(sum(command[0] == "BLOCK" for command in self.commands), 1)

    async def test_cancelled_operation_releases_client_connection(self):
        async def operation():
            async with RedisStore(RedisSettings(self.url)) as store:
                await store.client.execute_command("BLOCK")
        task = asyncio.create_task(operation())
        try:
            await asyncio.wait_for(self.blocked.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(self.closed.wait(), 3)

    async def test_cli_connection_failure_hides_server_error_and_credentials(self):
        self.ping_error = True
        env = {key: value for key, value in os.environ.items() if not key.startswith("EVALFORGE_REDIS_")}
        env["EVALFORGE_REDIS_URL"] = self.url
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", "-m", "evalforge.redis", "check", "--env-file", ".env.example",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        output = (stdout + stderr).decode("utf-8")
        self.assertEqual(process.returncode, 1)
        self.assertIn("ResponseError", output)
        self.assertNotIn("server-secret", output)
        self.assertNotIn(self.url, output)
        self.assertNotIn("p@ss", output)


@unittest.skipUnless(os.environ.get("EVALFORGE_TEST_REDIS_URL"), "No Redis test instance configured")
class RedisIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_redis_ping(self):
        async with RedisStore(RedisSettings(os.environ["EVALFORGE_TEST_REDIS_URL"])) as store:
            self.assertTrue(await store.check_connection())
