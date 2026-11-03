"""进程共享的异步 Redis 客户端；只管理资源，不封装业务键或自动重试。"""

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from .config import RedisSettings, connection_options


class RedisStore:
    def __init__(self, settings: RedisSettings):
        self.settings = settings
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Open RedisStore with async with before using it")
        return self._client

    async def __aenter__(self) -> "RedisStore":
        if self._client is not None:
            raise RuntimeError("RedisStore is already open")
        self._client = Redis(
            **connection_options(self.settings.url),
            decode_responses=True, protocol=2,
            socket_connect_timeout=self.settings.connect_timeout,
            socket_timeout=self.settings.socket_timeout,
            max_connections=self.settings.max_connections,
            health_check_interval=self.settings.health_check_interval,
            # 超时可能发生在服务端写入后；重试是否安全由具体业务决定。
            retry=Retry(NoBackoff(), 0),
            ssl_cert_reqs="required", ssl_check_hostname=True,
        )
        return self  # 客户端及池惰性连接；显式 PING 或首个命令才访问网络。

    async def check_connection(self) -> bool:
        return await self.client.ping()

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()  # 关闭内部所有连接；调用方先结束在途操作。

    async def __aexit__(self, *exc) -> None:
        await self.close()
