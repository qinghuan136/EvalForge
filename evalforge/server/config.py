"""HTTP 会话配置；数据库与 Redis 配置继续使用各自基础设施。"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def origin_of(value: str) -> str:
    """返回精确 origin；不信任请求中的 Host / 转发头来决定允许来源。"""
    try:
        url = urlsplit(value)
        port = url.port
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None
                or any(char.isspace() for char in value)):
            raise ValueError()
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = f":{port}" if port is not None and (url.scheme, port) not in (("http", 80), ("https", 443)) else ""
        return f"{url.scheme}://{host}{suffix}"
    except (ValueError, TypeError):
        raise ValueError("Invalid HTTP origin") from None


@dataclass(frozen=True)
class ServerSettings:
    allowed_origins: tuple[str, ...] = ("https://localhost:8000",)
    cookie_secure: bool = True
    session_ttl_seconds: int = 86400
    login_limit: int = 10
    register_limit: int = 5

    def __post_init__(self):
        if not self.allowed_origins or type(self.cookie_secure) is not bool:
            raise ValueError("Configure explicit HTTP origins and a boolean cookie_secure")
        for value in self.allowed_origins:
            url = urlsplit(value)
            if origin_of(value) != value or url.path or url.query or url.fragment:
                raise ValueError("Allowed origins must be exact origins without paths")
            if not self.cookie_secure and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Insecure cookies are only allowed for loopback development")
        for name in ("session_ttl_seconds", "login_limit", "register_limit"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"Invalid server {name}")

    @property
    def cookie_name(self) -> str:
        return "__Host-evalforge_session" if self.cookie_secure else "evalforge_session"

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "ServerSettings":
        values = {**dotenv_values(env_file), **os.environ}
        secure = values.get("EVALFORGE_COOKIE_SECURE", "true").lower()
        if secure not in ("true", "false"):
            raise ValueError("EVALFORGE_COOKIE_SECURE must be true or false")
        try:
            ttl = int(values.get("EVALFORGE_SESSION_TTL_SECONDS", "86400"))
        except (ValueError, TypeError):
            raise ValueError("Invalid EVALFORGE_SESSION_TTL_SECONDS") from None
        return cls(
            allowed_origins=tuple(value.strip() for value in values.get("EVALFORGE_ALLOWED_ORIGINS", "https://localhost:8000").split(",")),
            cookie_secure=secure == "true", session_ttl_seconds=ttl,
        )
