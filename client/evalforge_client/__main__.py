"""当前仅提供包信息；常驻客户端入口将在会话层完成后接入。"""

import argparse

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EvalForge Client：已实现传输和 JSON-RPC；完整 Runner 尚未接入。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()
