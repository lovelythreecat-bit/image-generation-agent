"""Parameterized queries and graph locking used inside caller-owned transactions."""

from sqlalchemy import select

from .settings import ServerError


class ConfigRepository:
    def __init__(self, session):
        self.session = session

    async def get(self, model, identifier, *, lock=True):
        query = select(model).where(model.id == identifier)
        if lock:
            query = query.with_for_update()
        value = (await self.session.execute(query)).scalar_one_or_none()
        if value is None:
            raise ServerError(404, "config_not_found", "配置不存在")
        return value

    async def list(self, model, limit=20, offset=0):
        query = select(model).order_by(model.created_at, model.id).limit(limit).offset(offset)
        return list((await self.session.execute(query)).scalars())

    async def referenced(self, model, column, identifier):
        query = select(model).where(column == identifier).limit(1).with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none() is not None
