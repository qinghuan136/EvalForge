"""单连接 hello/sync、接单门禁与结果上传；业务执行由独立 JobManager 接入。"""

import asyncio
import logging
from typing import Protocol

from . import __version__
from .attempts import AttemptRepository
from .config import ClientConfig
from .models import RunnerAttempt, TERMINAL_STATUSES
from .rpc import JsonRpcPeer

logger = logging.getLogger(__name__)


class SessionError(RuntimeError):
    """会话响应不合法或与本地事实冲突；关闭连接后重新对账。"""


class SessionJobs(Protocol):
    """后续 JobManager 接口；其执行任务必须独立于连接生命周期。"""
    capabilities: list[str]

    async def dispatch(self, params: dict) -> dict: ...
    async def resume(self, attempt: RunnerAttempt) -> None: ...
    async def cancel(self, attempt: RunnerAttempt) -> None: ...


class RunnerSession:
    def __init__(self, config: ClientConfig, attempts: AttemptRepository, jobs: SessionJobs | None = None):
        self.config, self.attempts, self.jobs = config, attempts, jobs
        self.session_id: str | None = None
        self.phase = "disconnected"

    @property
    def ready(self) -> bool:
        return self.phase == "ready"

    async def _dispatch(self, **params):
        if not self.ready:
            return {"accepted": False, "reason": "not_ready"}
        if self.jobs is None:
            return {"accepted": False, "reason": "executor_unavailable"}
        if await self.attempts.list_unresolved():
            return {"accepted": False, "reason": "busy"}
        # JobManager 仍须原子管理容量，并在持久化接收后返回。
        return await self.jobs.dispatch(params)

    async def _cancel(self, job_id: str, attempt_id: str, reason: str | None = None):
        if not self.ready:
            return {"accepted": False, "reason": "not_ready"}
        row = await self.attempts.get(job_id, attempt_id)
        if row is None:
            return {"accepted": False, "reason": "attempt_not_found"}
        await self._apply_cancel(row, reason)
        return {"accepted": True}

    async def _apply_cancel(self, row, reason=None):
        row = await self.attempts.request_cancel(row.job_id, row.attempt_id, reason)
        if row.execution_status == "accepted":
            await self.attempts.finish(row.job_id, row.attempt_id, {
                "job_id": row.job_id, "attempt_id": row.attempt_id, "status": "cancelled",
                "output": None, "duration_ms": 0, "http_status": None,
                "error": {"code": "cancelled", "message": "Cancelled before Agent invocation"},
            })
        elif row.execution_status == "running" and self.jobs is not None:
            await self.jobs.cancel(row)
        # unknown 保留未知事实和取消意图，不伪造远端已停止。

    async def run(self, peer: JsonRpcPeer):
        self.phase = "hello"
        peer.register("jobs.dispatch", self._dispatch)
        peer.register("jobs.cancel", self._cancel)
        try:
            hello = await peer.call("runner.hello", {
                "runner_id": self.config.runner_id, "version": __version__,
                "capabilities": self.jobs.capabilities if self.jobs is not None else [],
                "max_concurrent_jobs": 1,
            }, timeout=self.config.rpc_timeout)
            if (not isinstance(hello, dict) or not isinstance(hello.get("session_id"), str)
                    or not hello["session_id"].strip() or hello.get("status") != "ready"):
                raise SessionError("Invalid hello response")
            self.session_id, self.phase = hello["session_id"], "syncing"
            active, pending = await self.attempts.sync_snapshot()
            historical = {(r.job_id, r.attempt_id) for r in pending}
            snapshot = {(r.job_id, r.attempt_id): r for r in active + pending}
            reply = await peer.call("runner.sync", {
                "runner_id": self.config.runner_id,
                "active_jobs": [{"job_id": r.job_id, "attempt_id": r.attempt_id,
                                 "status": r.execution_status} for r in active],
                "pending_results": [r.result_json for r in pending],
            }, timeout=self.config.rpc_timeout)
            actions = self._validate_actions(reply, snapshot, historical)
            # 先完整校验所有指令，再应用；执行时重新读取，不能倒退同步期间变化的事实。
            for key, action in actions:
                row = await self.attempts.get(*key)
                if row is None:
                    raise SessionError("Attempt disappeared during synchronization")
                if action == "retry_upload":
                    await self._upload(peer, row)
                elif row.execution_status in TERMINAL_STATUSES:
                    continue  # 快照后的新结果在 ready 后正常上报。
                elif action == "cancel":
                    await self._apply_cancel(row, "cloud_sync")
                elif self.jobs is not None:
                    await self.jobs.resume(row)
            self.phase = "ready"
            logger.info("Runner session ready")
            await self._serve_ready(peer, historical)
        finally:
            self.phase, self.session_id = "disconnected", None

    @staticmethod
    def _validate_actions(reply, snapshot, historical):
        if not isinstance(reply, dict) or not isinstance(reply.get("jobs"), list):
            raise SessionError("Invalid sync response")
        actions, seen = [], set()
        for item in reply["jobs"]:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("job_id", "attempt_id", "action")):
                raise SessionError("Invalid sync instruction")
            key, action = (item["job_id"], item["attempt_id"]), item["action"]
            if key not in snapshot or key in seen or action not in ("continue", "cancel", "retry_upload"):
                raise SessionError("Unsupported, duplicate or unknown sync instruction")
            if key in historical:
                if action != "retry_upload":
                    raise SessionError("Pending result requires explicit retry_upload")
            elif action == "retry_upload" or (action == "continue" and snapshot[key].execution_status == "unknown"):
                raise SessionError("Sync action contradicts local state")
            seen.add(key)
            actions.append((key, action))
        if seen != set(snapshot):
            raise SessionError("Sync response omitted attempts")
        return actions

    async def _upload(self, peer, row):
        if row.delivery_status == "acknowledged":
            return
        if row.delivery_status != "pending" or row.result_json is None:
            raise SessionError("No persisted result to upload")
        ack = await peer.call("jobs.complete", row.result_json, timeout=self.config.rpc_timeout)
        if not isinstance(ack, dict) or ack.get("acknowledged") is not True:
            raise SessionError("Result was not acknowledged")
        await self.attempts.acknowledge(row.job_id, row.attempt_id)

    async def _serve_ready(self, peer, historical):
        async def upload_new_results():
            while True:
                for row in await self.attempts.list_pending_results():
                    if (row.job_id, row.attempt_id) not in historical:
                        await self._upload(peer, row)
                await asyncio.sleep(0.5)

        tasks = [asyncio.create_task(peer.wait_closed()), asyncio.create_task(upload_new_results())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
