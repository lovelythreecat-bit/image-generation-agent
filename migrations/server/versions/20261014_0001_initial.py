"""Initialize HTTP server metadata; compatible with MySQL 5.7 and 8.0."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "20261014_0001"
down_revision = None
branch_labels = None
depends_on = None

OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
    "mysql_row_format": "DYNAMIC",
}


def identity():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def versioned():
    return [
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "provider_services",
        *identity(),
        *versioned(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("generation_path", sa.String(512)),
        sa.Column("edit_path", sa.String(512)),
        sa.Column("chat_path", sa.String(512), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        **OPTIONS,
    )
    op.create_table(
        "model_configs",
        *identity(),
        *versioned(),
        sa.Column(
            "service_id",
            sa.String(36),
            sa.ForeignKey("provider_services.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("model", sa.String(300), nullable=False),
        sa.Column("description", sa.String(2000)),
        **OPTIONS,
    )
    op.create_index("ix_model_configs_service_id", "model_configs", ["service_id"])
    op.create_table(
        "agent_configs",
        *identity(),
        *versioned(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        **OPTIONS,
    )
    op.create_table(
        "agent_model_routes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "agent_config_id",
            sa.String(36),
            sa.ForeignKey("agent_configs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_config_id",
            sa.String(36),
            sa.ForeignKey("model_configs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("route_kind", sa.String(20), nullable=False),
        sa.Column("route_name", sa.String(100), nullable=False),
        sa.UniqueConstraint(
            "agent_config_id", "route_kind", "route_name", name="uq_agent_route_name"
        ),
        **OPTIONS,
    )
    op.create_index(
        "ix_agent_model_routes_agent_config_id", "agent_model_routes", ["agent_config_id"]
    )
    op.create_index(
        "ix_agent_model_routes_model_config_id", "agent_model_routes", ["model_config_id"]
    )
    op.create_table(
        "http_media",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("storage_ref", sa.String(512), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        **OPTIONS,
    )
    op.create_table(
        "http_tasks",
        *identity(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "agent_config_id",
            sa.String(36),
            sa.ForeignKey("agent_configs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("normalized_request", sa.JSON(), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.String(200).with_variant(mysql.VARBINARY(800), "mysql"),
            nullable=False,
            unique=True,
        ),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("config_versions", sa.JSON(), nullable=False),
        sa.Column("config_snapshot_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("core_status", sa.String(60)),
        sa.Column("progress", sa.JSON()),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("operation_input", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.Column("result_ref", sa.String(512)),
        sa.Column("run_ref", sa.String(512), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.String(500)),
        **OPTIONS,
    )
    op.create_index("ix_http_tasks_agent_config_id", "http_tasks", ["agent_config_id"])
    op.create_index("ix_http_tasks_status_created", "http_tasks", ["status", "created_at"])


def downgrade():
    for name in (
        "http_tasks",
        "http_media",
        "agent_model_routes",
        "agent_configs",
        "model_configs",
        "provider_services",
    ):
        op.drop_table(name)
