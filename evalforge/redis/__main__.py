"""python -m evalforge.redis check-config / check。"""

import argparse
import asyncio

from .config import RedisSettings
from .store import RedisStore


async def _check(settings: RedisSettings) -> None:
    async with RedisStore(settings) as store:
        if not await store.check_connection():
            raise RuntimeError("Unexpected Redis PING response")
    print("Redis connection OK (PING/PONG).")


def main() -> int:
    parser = argparse.ArgumentParser(description="EvalForge Redis connection tools")
    parser.add_argument("command", choices=("check-config", "check"))
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    try:
        settings = RedisSettings.from_env(args.env_file)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "check-config":
        print("Redis configuration OK; no connection was attempted.")
        return 0
    try:
        asyncio.run(_check(settings))
    except Exception as exc:
        # 不输出连接串、凭据或可能含服务端敏感信息的原始异常。
        print(f"Redis connection failed ({type(exc).__name__}).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
