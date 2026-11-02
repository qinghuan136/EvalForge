"""python -m evalforge_client --config client-config.json"""

import argparse
import asyncio
import logging

from . import __version__
from .app import run_client
from .config import ClientConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EvalForge Client：连接云端，执行单 Job HTTP 调用并回传结果。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="client-config.json")
    parser.add_argument("--check-config", action="store_true", help="只校验配置，不打开数据库或联网")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # INFO 请求日志可能包含带敏感 query 的 URL。
    try:
        config = ClientConfig.load(args.config)
        if args.check_config:
            print("Client configuration OK; no connection attempted.")
            return
        asyncio.run(run_client(config))
    except KeyboardInterrupt:
        pass
    except ValueError as exc:
        parser.error(str(exc))
    except Exception as exc:
        # 不打印驱动异常内容，可能含敏感参数。
        logging.error("Client stopped (%s)", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
