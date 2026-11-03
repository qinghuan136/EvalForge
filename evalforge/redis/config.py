"""Redis 配置；环境变量优先，不修改进程环境或隐式连接实例。"""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values


def connection_options(raw_url: str) -> dict:
    """只接受连接地址，不允许 URL query 覆盖 TLS、超时或重试选项。"""
    try:
        if (not isinstance(raw_url, str) or not raw_url or "?" in raw_url or "#" in raw_url
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw_url)
                or re.search(r"%(?![0-9A-Fa-f]{2})", raw_url)):
            raise ValueError()
        url = urlsplit(raw_url)
        port = url.port if url.port is not None else 6379
        if (url.scheme not in ("redis", "rediss") or not url.hostname or not 1 <= port <= 65535
                or (url.path not in ("", "/") and not re.fullmatch(r"/[0-9]+", url.path))
                or (url.username is not None and url.password is None)):
            raise ValueError()
        return {
            "host": url.hostname, "port": port, "db": int(url.path.lstrip("/") or "0"),
            "username": unquote(url.username or "", errors="strict") or None,
            "password": unquote(url.password, errors="strict") if url.password is not None else None,
            "ssl": url.scheme == "rediss",
        }
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid Redis URL; expected redis[s]://[user:password@]host[:port][/db], without query or fragment") from None


@dataclass(frozen=True)
class RedisSettings:
    url: str = field(repr=False)
    connect_timeout: float = 5
    socket_timeout: float = 5
    max_connections: int = 20
    health_check_interval: float = 30

    def __post_init__(self) -> None:
        connection_options(self.url)
        for name in ("connect_timeout", "socket_timeout", "health_check_interval"):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0 or (value == 0 and name != "health_check_interval")):
                raise ValueError(f"Invalid Redis {name}")
        if type(self.max_connections) is not int or self.max_connections <= 0:
            raise ValueError("Redis max_connections must be a positive integer")

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "RedisSettings":
        values = {**dotenv_values(env_file), **os.environ}
        raw_url = values.get("EVALFORGE_REDIS_URL")
        if not raw_url:
            raise ValueError("Set EVALFORGE_REDIS_URL in the environment or .env")
        try:
            options = {
                "connect_timeout": float(values.get("EVALFORGE_REDIS_CONNECT_TIMEOUT", "5")),
                "socket_timeout": float(values.get("EVALFORGE_REDIS_SOCKET_TIMEOUT", "5")),
                "max_connections": int(values.get("EVALFORGE_REDIS_MAX_CONNECTIONS", "20")),
                "health_check_interval": float(values.get("EVALFORGE_REDIS_HEALTH_CHECK_INTERVAL", "30")),
            }
        except (ValueError, TypeError, OverflowError):
            raise ValueError("Invalid Redis numeric configuration") from None
        return cls(url=raw_url, **options)
