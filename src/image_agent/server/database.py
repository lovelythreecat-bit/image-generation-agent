"""Explicit asynchronous database lifecycle; schema changes are external migrations."""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .settings import ServerError


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        if not await self.ready():
            raise ServerError(503, "database_unavailable", "数据库暂时不可用")

    async def ready(self):
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    async def close(self):
        await self.engine.dispose()
