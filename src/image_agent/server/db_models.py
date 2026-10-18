"""Server metadata. Core checkpoints remain in their existing SQLite store."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def new_id():
    return str(uuid4())


def utc_now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IdentityTimestamps:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Versioned:
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
    "mysql_row_format": "DYNAMIC",
}


class ExactKey(TypeDecorator):
    """MySQL text collations ignore trailing spaces; binary UTF-8 preserves keys."""

    impl = String(200)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(
            mysql.VARBINARY(800) if dialect.name == "mysql" else String(200)
        )

    def process_bind_param(self, value, dialect):
        return value.encode("utf-8") if value is not None and dialect.name == "mysql" else value

    def process_result_value(self, value, dialect):
        return value.decode("utf-8") if value is not None and dialect.name == "mysql" else value


class ProviderService(IdentityTimestamps, Versioned, Base):
    __tablename__ = "provider_services"
    __table_args__ = TABLE_OPTIONS
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(2048))
    protocol: Mapped[str] = mapped_column(String(32))
    generation_path: Mapped[str | None] = mapped_column(String(512))
    edit_path: Mapped[str | None] = mapped_column(String(512))
    chat_path: Mapped[str] = mapped_column(String(512), default="/chat/completions")
    api_key_encrypted: Mapped[str] = mapped_column(Text)


class ModelConfig(IdentityTimestamps, Versioned, Base):
    __tablename__ = "model_configs"
    __table_args__ = TABLE_OPTIONS
    service_id: Mapped[str] = mapped_column(
        ForeignKey("provider_services.id", ondelete="RESTRICT"), index=True
    )
    model: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(String(2000))


class AgentConfigRecord(IdentityTimestamps, Versioned, Base):
    __tablename__ = "agent_configs"
    __table_args__ = TABLE_OPTIONS
    name: Mapped[str] = mapped_column(String(200))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentModelRoute(Base):
    __tablename__ = "agent_model_routes"
    __table_args__ = (
        UniqueConstraint("agent_config_id", "route_kind", "route_name", name="uq_agent_route_name"),
        TABLE_OPTIONS,
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_config_id: Mapped[str] = mapped_column(
        ForeignKey("agent_configs.id", ondelete="CASCADE"), index=True
    )
    model_config_id: Mapped[str] = mapped_column(
        ForeignKey("model_configs.id", ondelete="RESTRICT"), index=True
    )
    route_kind: Mapped[str] = mapped_column(String(20))
    route_name: Mapped[str] = mapped_column(String(100))


class HttpMedia(Base):
    __tablename__ = "http_media"
    __table_args__ = TABLE_OPTIONS
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    storage_ref: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HttpTask(IdentityTimestamps, Base):
    __tablename__ = "http_tasks"
    __table_args__ = (Index("ix_http_tasks_status_created", "status", "created_at"), TABLE_OPTIONS)
    kind: Mapped[str] = mapped_column(String(32))
    agent_config_id: Mapped[str] = mapped_column(
        ForeignKey("agent_configs.id", ondelete="RESTRICT"), index=True
    )
    normalized_request: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(ExactKey(), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    config_versions: Mapped[dict] = mapped_column(JSON)
    config_snapshot_encrypted: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    core_status: Mapped[str | None] = mapped_column(String(60))
    progress: Mapped[dict | None] = mapped_column(JSON)
    operation: Mapped[str] = mapped_column(String(20), default="initial")
    operation_input: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    result_ref: Mapped[str | None] = mapped_column(String(512))
    run_ref: Mapped[str] = mapped_column(String(512))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(500))
