"""进程级单 Job 执行；只持久化结果，RPC 通知和确认仍由会话层负责。"""

import asyncio
import json
import logging
import math
from functools import partial
from time import monotonic

from .attempts import AttemptConflict, AttemptRepository
from .http_executor import HttpExecutor, HttpExecutionResult
from .models import RunnerAttempt, encode_json
from .rpc import RpcError

logger = logging.getLogger(__name__)


class JobManager:
    capabilities = ["http_json"]

    def __init__(self, attempts: AttemptRepository, executor: HttpExecutor):
        self.attempts, self.executor = attempts, executor
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._http_task: asyncio.Task | None = None
        self._cancel_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._key: tuple[str, str] | None = None
        self._closed = True
        self._failed = False

    async def __aenter__(self) -> "JobManager":
        if not self._closed:
            raise RuntimeError("JobManager is already open")
        self._closed = False
        return self

    async def __aexit__(self, *exc) -> None:
        logger.info("JobManager stopping active=%s", self._running())
        async with self._lock:
            self._closed = True
            self._stop_http_wait()
        # 不取消协调协程：已经拿到的结果须完成落盘；停止 HTTP 后的未知执行保留 running。
        tasks = list(self._cancel_tasks.values())
        if self._task is not None:
            tasks.append(self._task)
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("JobManager stopped")
        # 退出不伪造业务取消结果；遗留 running 由下次进程启动转成 unknown。

    @staticmethod
    def _parameters(params: dict) -> dict:
        required = {"job_id", "attempt_id", "agent_target_id", "example_id", "input", "timeout_seconds"}
        try:
            if not isinstance(params, dict) or params.keys() != required:
                raise ValueError()
            for name in ("job_id", "attempt_id", "agent_target_id", "example_id"):
                if not isinstance(params[name], str) or not params[name].strip():
                    raise ValueError()
            timeout = params["timeout_seconds"]
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError()
            values = dict(params)
            values["input_json"] = json.loads(encode_json(values.pop("input")))
            return values
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise RpcError(-32602, "Invalid dispatch params") from None

    async def dispatch(self, params: dict) -> dict:
        values = self._parameters(params)
        async with self._lock:
            if self._closed or self._failed:
                logger.warning("Dispatch rejected job_id=%r attempt_id=%r reason=executor_unavailable",
                               values["job_id"], values["attempt_id"])
                return {"accepted": False, "reason": "executor_unavailable"}
            row = await self.attempts.get(values["job_id"], values["attempt_id"])
            if row is not None:
                # 重复派发先于容量判断；仓储验证同一 ID 的参数必须完全一致。
                try:
                    row = await self.attempts.accept(**values)
                except AttemptConflict:
                    logger.warning("Dispatch rejected job_id=%r attempt_id=%r reason=attempt_conflict",
                                   row.job_id, row.attempt_id)
                    return {"accepted": False, "reason": "attempt_conflict"}
                if row.execution_status == "accepted":
                    self._schedule(row)
                logger.debug("Duplicate dispatch confirmed job_id=%r attempt_id=%r status=%s",
                             row.job_id, row.attempt_id, row.execution_status)
                return {"accepted": True}
            if not self.executor.has_target(values["agent_target_id"]):
                logger.warning("Dispatch rejected job_id=%r attempt_id=%r agent_target_id=%r reason=target_not_found",
                               values["job_id"], values["attempt_id"], values["agent_target_id"])
                return {"accepted": False, "reason": "target_not_found"}
            if self._running() or await self.attempts.list_unresolved():
                logger.debug("Dispatch rejected job_id=%r attempt_id=%r reason=busy",
                             values["job_id"], values["attempt_id"])
                return {"accepted": False, "reason": "busy"}
            row = await self.attempts.accept(**values)
            logger.info("Job accepted job_id=%r attempt_id=%r agent_target_id=%r",
                        row.job_id, row.attempt_id, row.agent_target_id)
            self._schedule(row)
            return {"accepted": True}  # 此时 accepted 已提交，不等待 HTTP 执行。

    def _running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _schedule(self, row: RunnerAttempt) -> None:
        key = (row.job_id, row.attempt_id)
        if self._running():
            if self._key != key:
                raise RpcError(-32000, "Runner capacity is occupied")
            return
        self._key = key
        self._task = asyncio.create_task(self._execute(row), name="evalforge-job")
        self._task.add_done_callback(partial(self._execution_done, key=key))

    def _execution_done(self, task: asyncio.Task, *, key: tuple[str, str]) -> None:
        error = None if task.cancelled() else task.exception()
        failed = not self._closed if task.cancelled() else error is not None
        if failed:
            self._failed = True
            logger.error("Job control failed; new dispatches disabled job_id=%r attempt_id=%r error_type=%s",
                         *key, "CancelledError" if task.cancelled() else type(error).__name__)

    async def _execute(self, row: RunnerAttempt) -> None:
        async with self._lock:
            if self._closed:
                return
            row = await self.attempts.get(row.job_id, row.attempt_id)
            if row.execution_status == "accepted" and row.cancel_requested:
                await self._finish_cancelled(row)
                return
            if not await self.attempts.claim(row.job_id, row.attempt_id):
                logger.debug("Job execution skipped job_id=%r attempt_id=%r reason=claim_not_acquired",
                             row.job_id, row.attempt_id)
                return
            logger.info("Job execution started job_id=%r attempt_id=%r agent_target_id=%r",
                        row.job_id, row.attempt_id, row.agent_target_id)
            # 与取消共享锁，保证 running 提交后、HTTP Task 建立前不会遗漏取消信号。
            started = monotonic()
            http_task = self._http_task = asyncio.create_task(self.executor.execute(
                row.agent_target_id, row.input_json, timeout_seconds=row.timeout_seconds,
            ), name="evalforge-agent-http")
        try:
            outcome = await http_task
        except asyncio.CancelledError:
            if self._closed:
                logger.warning("HTTP wait stopped during shutdown; execution unresolved job_id=%r attempt_id=%r",
                               row.job_id, row.attempt_id)
                raise  # 退出进程不等于用户取消；下次启动将 running 转为 unknown。
            current = await self.attempts.get(row.job_id, row.attempt_id)
            if not current.cancel_requested:
                raise
            await self._finish_cancelled(current, duration_ms=(monotonic() - started) * 1000)
            return
        except Exception as exc:
            logger.error("Unexpected executor failure job_id=%r attempt_id=%r error_type=%s",
                         row.job_id, row.attempt_id, type(exc).__name__)
            # 网络错误已由 HTTP 层结构化；本分支处理意外执行错误，不暴露异常内容。
            outcome = HttpExecutionResult(None, None, 0, {
                "code": "executor_error", "message": "Agent execution failed unexpectedly",
            })
        finally:
            self._http_task = None
        await self.attempts.finish(row.job_id, row.attempt_id, {
            "job_id": row.job_id, "attempt_id": row.attempt_id,
            "status": "succeeded" if outcome.error is None else "failed",
            "output": outcome.output, "duration_ms": outcome.duration_ms,
            "http_status": outcome.http_status, "error": outcome.error,
        })
        logger.log(logging.INFO if outcome.error is None else logging.WARNING,
                   "Job result saved job_id=%r attempt_id=%r status=%s delivery=pending http_status=%s duration_ms=%.1f error_code=%r",
                   row.job_id, row.attempt_id, "succeeded" if outcome.error is None else "failed",
                   outcome.http_status, outcome.duration_ms, None if outcome.error is None else outcome.error["code"])

    async def _finish_cancelled(self, row: RunnerAttempt, *, duration_ms: float = 0) -> None:
        await self.attempts.finish(row.job_id, row.attempt_id, {
            "job_id": row.job_id, "attempt_id": row.attempt_id, "status": "cancelled",
            "output": None, "duration_ms": duration_ms, "http_status": None,
            "error": {"code": "cancelled", "message": (
                "Client stopped waiting for Agent; the remote Agent may still be running"
                if row.started_at is not None or duration_ms > 0 else "Cancelled before Agent invocation"
            )},
        })
        logger.info("Job result saved job_id=%r attempt_id=%r status=cancelled delivery=pending duration_ms=%.1f",
                    row.job_id, row.attempt_id, duration_ms)

    def _stop_http_wait(self) -> None:
        # 清理响应流也可能 await；重复 cancel 不能再次打断它的 finally/aclose。
        if (self._http_task is not None and not self._http_task.done()
                and self._http_task.cancelling() == 0):
            self._http_task.cancel()

    async def resume(self, attempt: RunnerAttempt) -> None:
        """仅响应云端 continue：accepted 可启动，存活的 running 不重复执行。"""
        async with self._lock:
            row = await self.attempts.get(attempt.job_id, attempt.attempt_id)
            if row is None or self._closed or self._failed:
                raise RpcError(-32000, "Executor is unavailable")
            if row.execution_status == "accepted":
                if len(await self.attempts.list_unresolved()) != 1:
                    raise RpcError(-32000, "Runner capacity is occupied")
                logger.info("Resuming accepted attempt job_id=%r attempt_id=%r cancel_requested=%s",
                            row.job_id, row.attempt_id, bool(row.cancel_requested))
                self._schedule(row)
            elif row.execution_status == "running":
                if not self._running() or self._key != (row.job_id, row.attempt_id):
                    raise RpcError(-32000, "Active execution is unavailable")
                if row.cancel_requested:
                    self._stop_http_wait()
                logger.debug("Keeping active attempt job_id=%r attempt_id=%r", row.job_id, row.attempt_id)
            elif row.execution_status == "unknown":
                logger.warning("Unknown attempt cannot resume job_id=%r attempt_id=%r", row.job_id, row.attempt_id)
                raise RpcError(-32000, "Unknown attempts cannot be executed again")

    async def cancel(self, attempt: RunnerAttempt, reason: str | None = None) -> None:
        if reason is not None and not isinstance(reason, str):
            raise RpcError(-32602, "Invalid cancellation reason")
        if self._closed or self._failed:
            raise RpcError(-32000, "Executor is unavailable")
        key = (attempt.job_id, attempt.attempt_id)
        task = self._cancel_tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._apply_cancel(key, reason), name="evalforge-cancel")
            self._cancel_tasks[key] = task

            def done(completed):
                self._cancel_tasks.pop(key, None)
                self._execution_done(completed, key=key)

            task.add_done_callback(done)
        # RPC 处理协程因断线被取消，不能撤销已经开始的本地取消处理。
        await asyncio.shield(task)

    async def _apply_cancel(self, key: tuple[str, str], reason: str | None) -> None:
        async with self._lock:
            row = await self.attempts.request_cancel(*key, reason)
            logger.info("Cancellation handled job_id=%r attempt_id=%r status=%s cancel_requested=%s",
                        *key, row.execution_status, bool(row.cancel_requested))
            if row.execution_status == "accepted":
                await self._finish_cancelled(row)
            elif row.execution_status == "running":
                if self._key != key or not self._running():
                    raise RpcError(-32000, "Active execution is unavailable")
                self._stop_http_wait()
            # 终态不改写；unknown 仅保存取消意图，不伪造远端已停止。
