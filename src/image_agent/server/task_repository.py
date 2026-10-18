"""Short database transactions for submission, claims, and recovery."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .db_models import HttpTask
from .settings import ServerError


def now():
    return datetime.now(UTC)


class TaskRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.claim_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        try:
            async with self.session_factory() as session, session.begin():
                yield session
        except IntegrityError:
            raise
        except SQLAlchemyError:
            raise ServerError(503, "database_unavailable", "Task database is unavailable") from None

    async def get(self, task_id):
        async with self.transaction() as session:
            task = await session.get(HttpTask, task_id)
            if task is None:
                raise ServerError(404, "not_found", "Task was not found")
            session.expunge(task)
            return task

    async def claim(self):
        async with self.claim_lock, self.transaction() as session:
            # CAS remains protective on SQLite where FOR UPDATE is ignored.
            identifier = await session.scalar(
                select(HttpTask.id)
                .where(HttpTask.status == "queued")
                .order_by(HttpTask.created_at)
                .limit(1)
                .with_for_update()
            )
            if identifier is None:
                return None
            changed = await session.execute(
                update(HttpTask)
                .where(HttpTask.id == identifier, HttpTask.status == "queued")
                .values(status="running", updated_at=now())
            )
            if changed.rowcount != 1:
                return None
            task = await session.get(HttpTask, identifier)
            session.expunge(task)
            return task

    async def interrupt_running(self):
        async with self.transaction() as session:
            await session.execute(
                update(HttpTask)
                .where(HttpTask.status == "running")
                .values(
                    status="interrupted",
                    error_code="interrupted",
                    error_message="Execution stopped; explicit recovery is required",
                    updated_at=now(),
                )
            )

    async def finish(self, task_id, **values):
        async with self.transaction() as session:
            await session.execute(
                update(HttpTask)
                .where(HttpTask.id == task_id, HttpTask.status == "running")
                .values(**values, updated_at=now())
            )

    async def requeue(self, task_id, operation, operation_input=None, capacity=100):
        async with self.transaction() as session:
            status = await session.scalar(
                select(HttpTask.status).where(HttpTask.id == task_id).with_for_update()
            )
            if status not in ("completed", "failed", "interrupted"):
                raise ServerError(409, "task_active", "Task is already queued or running")
            count = await session.scalar(
                select(func.count())
                .select_from(HttpTask)
                .where(HttpTask.status.in_(("queued", "running")))
            )
            if count >= capacity:
                raise ServerError(429, "queue_full", "Task queue is full")
            changed = await session.execute(
                update(HttpTask)
                .where(
                    HttpTask.id == task_id,
                    HttpTask.status.in_(("completed", "failed", "interrupted")),
                )
                .values(
                    status="queued",
                    operation=operation,
                    operation_input=operation_input,
                    error_code=None,
                    error_message=None,
                    updated_at=now(),
                )
            )
            if changed.rowcount != 1:
                raise ServerError(409, "task_active", "Task is already queued or running")
