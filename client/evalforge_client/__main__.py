"""python -m evalforge_client --config client-config.json"""

import argparse
import asyncio
import logging

from . import __version__
from .app import run_client
from .config import ClientConfig

logger = logging.getLogger("evalforge_client.cli")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EvalForge Client：连接云端，执行单 Job HTTP 调用并回传结果。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="client-config.json")
    parser.add_argument("--check-config", action="store_true", help="只校验配置，不打开数据库或联网")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
                        default="INFO", help="客户端日志级别，默认 INFO")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    # DEBUG 只对客户端生效，避免第三方库输出请求头、URL、SQL 参数或消息正文。
    logging.getLogger("evalforge_client").setLevel(args.log_level)
    try:
        config = ClientConfig.load(args.config)
        if args.check_config:
            print("Client configuration OK; no connection attempted.")
            return
        asyncio.run(run_client(config))
    except KeyboardInterrupt:
        logger.info("Client interrupted")
    except ValueError as exc:
        parser.error(str(exc))
    except Exception as exc:
        # 不打印驱动异常内容，可能含敏感参数。
        logger.error("Client stopped error_type=%s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
