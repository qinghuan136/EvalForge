"""仅演示本地 ORM 持久化；不调用 Agent，也不模拟云端确认。"""

import asyncio

from evalforge_client.attempts import AttemptRepository
from evalforge_client.database import ClientDatabase


async def main():
    async with ClientDatabase("state/attempts-demo.sqlite3") as database:
        attempts = AttemptRepository(database)
        await attempts.accept(
            job_id="demo-job", attempt_id="demo-attempt", agent_target_id="demo-agent",
            example_id="demo-example", input_json={"message": "你好"}, timeout_seconds=30,
        )
        if await attempts.claim("demo-job", "demo-attempt"):
            # 示例值，不执行 HTTP。真实代码必须在 claim 返回 True 后才调用 Agent。
            await attempts.finish("demo-job", "demo-attempt", {
                "job_id": "demo-job", "attempt_id": "demo-attempt", "status": "succeeded",
                "output": {"reply": "本地存储示例"}, "duration_ms": 0, "http_status": None, "error": None,
            })
        for row in await attempts.list_pending_results():
            print(row.job_id, row.attempt_id, row.execution_status, row.delivery_status, row.result_json)


if __name__ == "__main__":
    asyncio.run(main())
