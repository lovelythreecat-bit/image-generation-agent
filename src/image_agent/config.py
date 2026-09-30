import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from .errors import ConfigurationError
from .models import Model

VISION_STAGES = {
    "vision",
    "describe_style",
    "analyze_materials",
    "validate_evidence",
    "extract_garments",
    "audit_image",
    "review_product",
    "audit_detail_set",
}
LEGACY_FIELDS = {
    "openrouter_api_key",
    "openrouter_base_url",
    "model_pro",
    "model_fast",
    "model_base",
    "quality_model",
    "style_model",
}


class ServiceConfig(Model):
    base_url: str
    protocol: Literal["openrouter_images", "openai_images", "chat_completions"]
    api_key: SecretStr | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    generation_path: str | None = None
    edit_path: str | None = None
    chat_path: str = "/chat/completions"

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an HTTP(S) URL without credentials, query or fragment"
            )
        return value.rstrip("/")

    @field_validator("generation_path", "edit_path", "chat_path")
    @classmethod
    def validate_path(cls, value):
        if value is not None and (
            not value.startswith("/")
            or value.startswith("//")
            or any(c in value for c in ("?", "#", "\\"))
            or ".." in value.split("/")
        ):
            raise ValueError("endpoint must be an absolute path on the configured service")
        return value

    @model_validator(mode="after")
    def single_key_source(self):
        if self.api_key is not None and self.api_key_env is not None:
            raise ValueError("choose api_key or api_key_env, never both")
        return self

    def credential(self):
        key = (
            os.environ.get(self.api_key_env, "")
            if self.api_key_env
            else self.api_key.get_secret_value()
            if self.api_key
            else ""
        )
        if not key.strip():
            raise ConfigurationError(f"{self.api_key_env or 'service api_key'} is required")
        return key


class ModelRoute(Model):
    service: str = Field(min_length=1)
    model: str = Field(min_length=1)


class AgentConfig(Model):
    services: dict[str, ServiceConfig] = Field(default_factory=dict)
    image_models: dict[Literal["pro", "fast", "base"], ModelRoute] = Field(default_factory=dict)
    stages: dict[str, ModelRoute] = Field(default_factory=dict)
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_pro: str = "google/gemini-3.1-flash-image"
    model_fast: str = "openai/gpt-image-2"
    model_base: str = "qwen/qwen-image-3-pro"
    quality_model: str = "openai/gpt-4o-mini"
    style_model: str | None = None
    http_proxy: str | None = None
    request_timeout_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    model_concurrency: int = Field(default=2, gt=0)
    generation_reference_limit: int = Field(default=4, gt=0)
    vision_image_limit: int = Field(default=12, gt=0)

    @model_validator(mode="after")
    def defaults(self):
        if self.services or self.image_models or self.stages:
            if self.model_fields_set & LEGACY_FIELDS:
                raise ValueError("structured configuration cannot mix legacy provider fields")
            if not self.image_models or "vision" not in self.stages:
                raise ValueError("structured configuration requires image_models and stages.vision")
            if set(self.stages) - VISION_STAGES:
                raise ValueError("unknown vision stage")
            for name, route in [*self.image_models.items(), *self.stages.items()]:
                if route.service not in self.services:
                    raise ValueError("route names an unknown service")
                protocol = self.services[route.service].protocol
                if (name in self.image_models) == (protocol == "chat_completions"):
                    raise ValueError("route service protocol does not support this stage")
        if self.style_model is None:
            object.__setattr__(self, "style_model", self.quality_model)
        return self

    @classmethod
    def from_env(cls):
        return cls(
            **{
                f: os.environ[f"IMAGE_AGENT_{f.upper()}"]
                for f in cls.model_fields
                if f not in {"services", "image_models", "stages"}
                if os.environ.get(f"IMAGE_AGENT_{f.upper()}")
            }
        )

    @classmethod
    def from_mapping(cls, data):
        try:
            return cls.model_validate(data)
        except ValidationError as error:
            # Pydantic's normal rendering contains input values, potentially secrets.
            details = "; ".join(
                ".".join(map(str, item["loc"])) + ": " + item["msg"]
                for item in error.errors(include_input=False, include_context=False)
            )
            raise ConfigurationError("invalid API configuration: " + details) from None

    @classmethod
    def from_file(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            raise ConfigurationError("cannot read API configuration as a JSON object") from None
        return cls.from_mapping(data)

    def service_names(self, image_alias=None):
        names = {route.service for route in self.stages.values()}
        if image_alias is not None and self.services:
            self.model_name(image_alias)
            names.add(self.image_models[image_alias].service)
        return names

    def validate_credentials(self, services=None):
        if self.services:
            if services is None:
                services = {r.service for r in [*self.image_models.values(), *self.stages.values()]}
            for name in sorted(services):
                self.services[name].credential()
        elif not self.openrouter_api_key or not self.openrouter_api_key.get_secret_value().strip():
            raise ConfigurationError("IMAGE_AGENT_OPENROUTER_API_KEY is required")

    def vision_route(self, stage):
        if stage not in VISION_STAGES:
            raise ConfigurationError("unknown vision stage")
        return self.stages.get(stage, self.stages.get("vision"))

    def model_name(self, alias):
        if alias not in ("pro", "fast", "base"):
            raise ConfigurationError("unknown image model alias")
        if self.image_models:
            if alias not in self.image_models:
                raise ConfigurationError(f"image model alias '{alias}' is not configured")
            return self.image_models[alias].model
        return getattr(self, f"model_{alias}")
