"""进程级启动恢复与连接循环；断线不会销毁数据库或 JobManager。"""

import logging
from contextlib import aclosing

from .attempts import AttemptRepository
from .config import ClientConfig
from .database import ClientDatabase
from .rpc import JsonRpcPeer, RpcError, RpcProtocolError
from .session import RunnerSession, SessionError, SessionJobs
from .transport import TransportClosed

logger = logging.getLogger(__name__)


async def run_client(config: ClientConfig, *, jobs: SessionJobs | None = None, transport=None):
    # TODO: 按需增加进程独占锁和 Runner/Cloud 数据库身份绑定。
    # 当前约定同一数据库只启动一个 Runner 进程。
    async with ClientDatabase(config.state_path) as database:
        attempts = AttemptRepository(database)
        recovered = await attempts.recover_interrupted()
        logger.info("Client initialized; interrupted attempts recovered: %s", recovered)
        transport = transport if transport is not None else config.transport()
        async with aclosing(transport.connections()) as connections:
            async for connection in connections:
                session = RunnerSession(config, attempts, jobs)
                try:
                    async with JsonRpcPeer(connection) as peer:
                        await session.run(peer)
                except (TransportClosed, TimeoutError, SessionError, RpcError, RpcProtocolError):
                    # 不记录远端错误详情、URL 或配置，避免泄露凭据和业务内容。
                    logger.warning("Session ended; reconnecting and synchronizing")
