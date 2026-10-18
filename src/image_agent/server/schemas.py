"""Configuration write/read DTOs. Read schemas never expose credential payloads."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


Protocol = Literal["openrouter_images", "openai_images", "chat_completions"]
NonEmpty = Annotated[str, Field(min_length=1, max_length=200)]


class ServiceCreate(StrictDTO):
    name: NonEmpty = Field(description="服务名称", examples=["图片服务"])
    base_url: str = Field(
        max_length=2048,
        description="无凭据和查询参数的 HTTP(S) 服务地址",
        examples=["https://api.example.com/v1"],
    )
    protocol: Protocol = Field(description="供应商调用协议")
    api_key: SecretStr = Field(
        min_length=1,
        repr=False,
        description="仅写入的 API Key；保存时加密",
        json_schema_extra={"writeOnly": True},
        examples=["replace-with-provider-key"],
    )
    generation_path: str | None = Field(default=None, max_length=512, description="图片生成端点")
    edit_path: str | None = Field(default=None, max_length=512, description="图片编辑端点")
    chat_path: str = Field(default="/chat/completions", max_length=512, description="视觉调用端点")
    enabled: bool = Field(default=True, description="是否可供新任务使用")

    @field_validator("api_key")
    @classmethod
    def nonblank_key(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("API Key 不能为空")
        return value


class ServicePatch(StrictDTO):
    version: int = Field(ge=1, description="当前版本；陈旧版本返回 409")
    name: NonEmpty | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    protocol: Protocol | None = None
    api_key: SecretStr = Field(
        default=None,
        min_length=1,
        repr=False,
        description="省略则保留；非空值替换；null 或空字符串拒绝",
        json_schema_extra={"writeOnly": True},
    )
    generation_path: str | None = Field(default=None, max_length=512)
    edit_path: str | None = Field(default=None, max_length=512)
    chat_path: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None

    @field_validator("api_key")
    @classmethod
    def nonblank_key(cls, value):
        return ServiceCreate.nonblank_key(value)


class ModelConfigCreate(StrictDTO):
    service_id: str = Field(min_length=1, max_length=36, description="所关联的服务 UUID")
    model: str = Field(
        min_length=1, max_length=300, description="供应商模型名称", examples=["gpt-image-2"]
    )
    description: str | None = Field(default=None, max_length=2000, description="模型用途说明")
    enabled: bool = True


class ModelConfigPatch(StrictDTO):
    version: int = Field(ge=1, description="当前版本")
    service_id: str | None = Field(default=None, min_length=1, max_length=36)
    model: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class AgentParameters(StrictDTO):
    http_proxy: str | None = Field(default=None, description="服务端显式代理配置")
    request_timeout_seconds: float = Field(
        default=180, gt=0, allow_inf_nan=False, description="模型请求超时秒数"
    )
    model_concurrency: int = Field(default=2, gt=0, description="核心模型并发上限")
    generation_reference_limit: int = Field(default=4, gt=0, description="生图参考图片上限")
    vision_image_limit: int = Field(default=12, gt=0, description="视觉调用图片上限")
    model_completion_attempts: int = Field(default=4, ge=1, le=8, description="模型补齐尝试上限")


class AgentConfigCreate(StrictDTO):
    name: NonEmpty = Field(description="配置方案名称")
    parameters: AgentParameters = Field(
        default_factory=AgentParameters, description="复用核心 AgentConfig 的运行参数"
    )
    image_models: dict[Literal["pro", "fast", "base"], str] = Field(
        description="图片模型别名到 model_config_id；至少一个别名",
        min_length=1,
        examples=[{"fast": "00000000-0000-0000-0000-000000000001"}],
    )
    stages: dict[str, str] = Field(
        description="现有视觉阶段到 model_config_id；必须包含 vision",
        min_length=1,
        examples=[{"vision": "00000000-0000-0000-0000-000000000002"}],
    )
    enabled: bool = True


class AgentConfigPatch(StrictDTO):
    version: int = Field(ge=1, description="当前版本")
    name: NonEmpty | None = None
    parameters: AgentParameters | None = Field(
        default=None, description="替换运行参数；未传字段使用核心默认值"
    )
    image_models: dict[Literal["pro", "fast", "base"], str] | None = Field(
        default=None, min_length=1
    )
    stages: dict[str, str] | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class ConfigRead(StrictDTO):
    id: str
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ServiceRead(ConfigRead):
    name: str
    base_url: str
    protocol: Protocol
    generation_path: str | None
    edit_path: str | None
    chat_path: str
    has_api_key: bool = Field(description="是否已存储加密凭据；不返回明文或密文")


class ModelConfigRead(ConfigRead):
    service_id: str
    model: str
    description: str | None


class AgentConfigRead(ConfigRead):
    name: str
    parameters: AgentParameters
    image_models: dict[str, str]
    stages: dict[str, str]


class ErrorResponse(StrictDTO):
    code: str = Field(description="稳定错误分类")
    message: str = Field(description="已脱敏的错误摘要")
    request_id: str = Field(description="请求追踪标识")
