"""python -m evalforge.db check-config / check。"""

import argparse
import asyncio

from .config import DatabaseSettings
from .engine import Database


async def _check(settings: DatabaseSettings) -> None:
    async with Database(settings) as db:
        name = await db.check_connection()
        print(f"PostgreSQL connection OK; database={name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="EvalForge PostgreSQL connection tools")
    parser.add_argument("command", choices=("check-config", "check"))
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    try:
        settings = DatabaseSettings.from_env(args.env_file)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "check-config":
        print(f"Configuration OK: {settings.safe_url}")
        print("No database connection was attempted.")
        return 0
    try:
        asyncio.run(_check(settings))
    except Exception as exc:
        # 驱动原始异常可能含连接细节，只输出错误类别。
        print(f"PostgreSQL connection failed ({type(exc).__name__}).")
        print("Check the instance, network, credentials and database name.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
