"""本地 Agent 目标配置；云端只能选择 ID，不能指定 URL 或请求头。"""

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class AgentTarget:
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)

    @classmethod
    def from_config(cls, config: dict) -> "AgentTarget":
        if not isinstance(config, dict) or config.keys() - {"url", "headers", "headers_env"}:
            raise ValueError("Invalid Agent target configuration")
        endpoint = config.get("url")
        try:
            if not isinstance(endpoint, str):
                raise ValueError()
            url = urlsplit(endpoint)
            if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                    or url.password is not None or url.fragment or url.port == 0):
                raise ValueError()
            endpoint = str(httpx.URL(endpoint))
        except (ValueError, TypeError, httpx.InvalidURL):
            raise ValueError("Invalid Agent target URL") from None
        headers = {}
        reserved = {"host", "content-length", "transfer-encoding", "connection", "content-type", "accept-encoding"}
        for source, from_env in ((config.get("headers", {}), False), (config.get("headers_env", {}), True)):
            if not isinstance(source, dict):
                raise ValueError("Agent headers must be an object")
            for name, value in source.items():
                if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                        or name.lower() in reserved or name.lower() in headers or not isinstance(value, str)):
                    raise ValueError("Invalid, duplicate or reserved Agent header")
                if from_env:
                    value = os.environ.get(value, "")
                    if not value.strip():
                        raise ValueError("Agent header environment variable is missing or empty")
                if "\r" in value or "\n" in value or "\x00" in value:
                    raise ValueError("Invalid Agent header value")
                headers[name.lower()] = value
        try:
            httpx.Headers(headers)
        except (UnicodeError, ValueError):
            raise ValueError("Invalid Agent headers") from None
        return cls(endpoint, headers)
