"""API 测试共享资源；生产事务实现配合异步 SQLite 和模拟 Redis。"""

import unittest

import fakeredis.aioredis
import httpx
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from evalforge.db import Database
from evalforge.db.models import Base, User
from evalforge.server.app import create_app
from evalforge.server.config import ServerSettings

ORIGIN = "https://testserver"
PASSWORD = "Correct horse battery staple"


class SQLiteDatabase(Database):
    def __init__(self, url="sqlite+aiosqlite:///:memory:"):
        self.engine = create_async_engine(url)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")


class APITestCase(unittest.IsolatedAsyncioTestCase):
    def make_database(self):
        return SQLiteDatabase()

    async def asyncSetUp(self):
        self.database = self.make_database()
        self.addAsyncCleanup(self.database.close)
        async with self.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.addAsyncCleanup(self.redis.aclose)
        self.config = ServerSettings(allowed_origins=(ORIGIN,), runner_websocket_url="wss://cloud.example/api/v1/runner/ws")
        self.app = create_app(self.config, database=self.database, redis_client=self.redis)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.client = self.new_client()

    def new_client(self):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                   base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.addAsyncCleanup(client.aclose)
        return client

    async def csrf(self, client=None):
        client = client or self.client
        response = await client.get("/api/v1/auth/csrf")
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response

    async def register(self, username="alice", client=None):
        client = client or self.client
        await self.csrf(client)
        response = await client.post("/api/v1/auth/register", json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 201, response.text)
        return response

    async def login(self, client=None, password=PASSWORD, username="alice"):
        client = client or self.client
        await self.csrf(client)
        response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response

    async def user(self):
        async with self.database.transaction() as db:
            return await db.scalar(select(User).where(User.username == "alice"))
