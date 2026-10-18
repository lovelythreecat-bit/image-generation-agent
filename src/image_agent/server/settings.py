"""Explicit startup configuration and safe public service errors."""
import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("服务配置包含重复字段")
        result[key] = value
    return result


class ServerError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ServerSettings:
    database_url: str = field(repr=False)
    token: str = field(repr=False)
    secret_key: str = field(repr=False)
    data_dir: Path
    worker_concurrency: int = 1
    queue_capacity: int = 100
    root_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    max_upload_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 60_000_000

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in
                   (self.database_url, self.token, self.secret_key)):
            raise ValueError("服务数据库、Token 和加密密钥必须显式提供")
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        for name in ("worker_concurrency", "queue_capacity", "port",
                     "max_upload_bytes", "max_image_pixels"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须为正整数")
        if self.port > 65535:
            raise ValueError("port 必须在 1–65535 范围内")
        if self.root_path and (not self.root_path.startswith("/") or
                               self.root_path.endswith("/") or
                               any(v in self.root_path for v in ("?", "#", "\\")) or
                               ".." in self.root_path.split("/")):
            raise ValueError("root_path 必须为不带末尾斜杠的绝对路径前缀")

    @classmethod
    def from_file(cls, path: str | Path = "server.config.json"):
        """Load only the selected JSON; relative storage belongs to its directory."""
        try:
            source = Path(path).resolve()
            values = json.loads(source.read_text(encoding="utf-8-sig"),
                                object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, ValueError):
            raise ValueError(
                "无法读取服务配置 JSON；请复制 server.config.example.json 为 server.config.json，"
                "或使用 --config 指定有效文件"
            ) from None
        try:
            if not isinstance(values, dict) or set(values) - {f.name for f in fields(cls)}:
                raise ValueError
            for name in ("database_url", "token", "secret_key", "data_dir"):
                if not isinstance(values.get(name), str) or not values[name].strip():
                    raise ValueError
            for name in ("host", "root_path"):
                if name in values and not isinstance(values[name], str):
                    raise ValueError
            if "host" in values and not values["host"].strip():
                raise ValueError
            parsed = urlsplit(values["database_url"])
            if parsed.scheme != "mysql+asyncmy" or not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError
            # Reject malformed ports and keys before opening any external resource.
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
            from cryptography.fernet import Fernet

            Fernet(values["secret_key"].encode())
            values["data_dir"] = (source.parent / values["data_dir"]).resolve()
            return cls(**values)
        except (TypeError, ValueError, OSError):
            raise ValueError(
                "服务配置字段无效；请检查必填字段、MySQL 地址、Fernet 密钥、路径和参数类型"
            ) from None

    @classmethod
    def from_env(cls):
        prefix = "IMAGE_AGENT_SERVER_"
        required = ("DATABASE_URL", "TOKEN", "SECRET_KEY", "DATA_DIR")
        missing = [name for name in required if not os.environ.get(prefix + name, "").strip()]
        if missing:
            raise ValueError("缺少必要服务环境变量：" + ", ".join(prefix + n for n in missing))
        database_url = os.environ[prefix + "DATABASE_URL"]
        try:
            parsed = urlsplit(database_url)
            valid = parsed.scheme == "mysql+asyncmy" and parsed.hostname and parsed.path.strip("/")
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("IMAGE_AGENT_SERVER_DATABASE_URL 必须是 mysql+asyncmy 的 MySQL 地址")
        values = {name.lower(): os.environ[prefix + name] for name in required}
        for name in ("WORKER_CONCURRENCY", "QUEUE_CAPACITY", "PORT", "MAX_UPLOAD_BYTES", "MAX_IMAGE_PIXELS"):
            if prefix + name in os.environ:
                try:
                    values[name.lower()] = int(os.environ[prefix + name])
                except ValueError:
                    raise ValueError(prefix + name + " 必须为正整数") from None
        for name in ("ROOT_PATH", "HOST"):
            if prefix + name in os.environ:
                values[name.lower()] = os.environ[prefix + name]
        return cls(**values)
