"""环境变量优先，其次读取指定 .env；没有配置时明确报错。"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


@dataclass(frozen=True)
class DatabaseSettings:
    url: URL = field(repr=False)
    echo: bool = False

    def __post_init__(self) -> None:
        if self.url.drivername != "postgresql+asyncpg":
            raise ValueError("DatabaseSettings requires the postgresql+asyncpg driver")
        if not self.url.host or not self.url.database:
            raise ValueError("PostgreSQL host and database name are required")
        try:
            port = self.url.port
        except ValueError:
            raise ValueError("PostgreSQL port must be an integer") from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("PostgreSQL port must be between 1 and 65535")

    @property
    def safe_url(self) -> str:
        return self.url.render_as_string(hide_password=True)

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "DatabaseSettings":
        values = {**dotenv_values(env_file), **os.environ}
        raw_url = values.get("EVALFORGE_DATABASE_URL")
        if not raw_url:
            raise ValueError("Set EVALFORGE_DATABASE_URL in the environment or .env")
        try:
            url = make_url(raw_url)
        except (ArgumentError, ValueError):
            raise ValueError("EVALFORGE_DATABASE_URL is not a valid SQLAlchemy database URL") from None
        if url.drivername in ("postgres", "postgresql"):
            url = url.set(drivername="postgresql+asyncpg")
        echo_value = (values.get("EVALFORGE_DB_ECHO") or "false").strip().lower()
        if echo_value not in ("true", "false", "1", "0"):
            raise ValueError("EVALFORGE_DB_ECHO must be true, false, 1 or 0")
        return cls(url=url, echo=echo_value in ("true", "1"))
