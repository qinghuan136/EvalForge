"""客户端配置；凭据仅从环境读取，相对状态路径按配置文件目录解析。"""

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from .transport import WebSocketTransport
from .targets import AgentTarget


@dataclass(frozen=True)
class ClientConfig:
    cloud_url: str = field(repr=False)
    runner_id: str
    token: str = field(repr=False)
    state_path: Path
    targets: dict = field(default_factory=dict, repr=False)
    rpc_timeout: float = 30
    allow_insecure_localhost: bool = False

    def __post_init__(self):
        if not isinstance(self.runner_id, str) or not self.runner_id.strip():
            raise ValueError("runner_id must be a non-empty string")
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("Runner token environment variable is missing or empty")
        if type(self.rpc_timeout) not in (int, float) or not math.isfinite(self.rpc_timeout) or self.rpc_timeout <= 0:
            raise ValueError("rpc_timeout must be finite and positive")
        if type(self.allow_insecure_localhost) is not bool:
            raise ValueError("allow_insecure_localhost must be a boolean")
        if not isinstance(self.targets, dict):
            raise ValueError("targets must be an object")
        for name, target in self.targets.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(target, dict):
                raise ValueError("Invalid Agent target configuration")
            AgentTarget.from_config(target)
        self.transport()  # 复用传输层的 WSS、凭据及 URL 校验。

    def transport(self) -> WebSocketTransport:
        return WebSocketTransport(self.cloud_url, bearer_token=self.token,
                                  allow_insecure_localhost=self.allow_insecure_localhost)

    @classmethod
    def load(cls, path: str | Path) -> "ClientConfig":
        path = Path(path).resolve()
        try:
            values = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise ValueError("Cannot read valid client JSON configuration") from None
        if not isinstance(values, dict):
            raise ValueError("Client configuration must be an object")
        allowed = {"cloud_url", "runner_id", "token_env", "state_path", "targets", "rpc_timeout", "allow_insecure_localhost"}
        if values.keys() - allowed:
            raise ValueError("Unknown client configuration field")
        for name in ("cloud_url", "runner_id", "token_env", "state_path"):
            if not isinstance(values.get(name), str) or not values[name].strip():
                raise ValueError(f"Missing or invalid {name}")
        state = Path(values["state_path"]).expanduser()
        return cls(
            cloud_url=values["cloud_url"], runner_id=values["runner_id"],
            token=os.environ.get(values["token_env"], ""),
            state_path=(path.parent / state).resolve(), targets=values.get("targets", {}),
            rpc_timeout=values.get("rpc_timeout", 30),
            allow_insecure_localhost=values.get("allow_insecure_localhost", False),
        )
