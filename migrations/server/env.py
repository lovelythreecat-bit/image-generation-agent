"""Migrations run explicitly, never as part of package import or server startup."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from image_agent.server.db_models import Base
from image_agent.server.settings import ServerSettings

config = context.config
target_metadata = Base.metadata


def database_url():
    # Explicit programmatic overrides remain available to isolated integration tests.
    if config.attributes.get("database_url"):
        return config.attributes["database_url"]
    path = context.get_x_argument(as_dictionary=True).get("config", "server.config.json")
    return ServerSettings.from_file(path).database_url


def run_sync(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = create_async_engine(database_url(), poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    try:
        asyncio.run(run_online())
    except Exception:
        raise RuntimeError(
            "Database migration failed; check database availability and migration prerequisites"
        ) from None
