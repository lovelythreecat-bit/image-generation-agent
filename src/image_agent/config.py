import os

from pydantic import Field, SecretStr, model_validator

from .models import Model


class AgentConfig(Model):
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
        if self.style_model is None:
            object.__setattr__(self, "style_model", self.quality_model)
        return self

    @classmethod
    def from_env(cls):
        return cls(**{f: os.environ[f"IMAGE_AGENT_{f.upper()}"] for f in cls.model_fields if os.environ.get(f"IMAGE_AGENT_{f.upper()}")})

    def model_name(self, alias):
        if alias not in ("pro", "fast", "base"):
            from .errors import ConfigurationError
            raise ConfigurationError("unknown image model alias")
        return getattr(self, f"model_{alias}")
