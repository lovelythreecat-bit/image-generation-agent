"""Execution limits and durable accounting at the actual HTTP submission boundary."""

import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .errors import AgentError, ConfigurationError


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_quality_repairs: int = Field(default=2, ge=0, le=10)
    max_audit_retries: int = Field(default=2, ge=0)
    max_image_calls: int = Field(default=100, ge=0)
    max_vision_calls: int = Field(default=200, ge=0)
    shared_ledger_path: Path | None = None
    shared_max_image_calls: int = Field(default=10, ge=0)
    shared_max_vision_calls: int = Field(default=30, ge=0)


class BudgetLedger:
    """Reservations never refund: even a lost response may have been charged.

    Attached SQLite databases participate in the same rollback-journal transaction,
    so per-run and optional shared reservations succeed or fail together.
    """

    def __init__(self, path, policy, *, require_existing=False):
        self.policy = policy
        self.lock = threading.Lock()
        path = Path(path).resolve() if path else None
        fresh = self._is_fresh(path, require_existing)
        self.db = sqlite3.connect(
            path.as_uri() + "?mode=rw" if path and not fresh else str(path) if path else ":memory:",
            uri=bool(path and not fresh),
            check_same_thread=False,
        )
        try:
            self.db.execute("PRAGMA journal_mode=DELETE")
            self._initialize("main", policy.max_image_calls, policy.max_vision_calls, fresh)
            if path:
                self._marker(path).touch(exist_ok=True)
            if policy.shared_ledger_path:
                shared = policy.shared_ledger_path.resolve()
                if shared == path:
                    raise ConfigurationError("shared budget must use a separate ledger")
                shared.parent.mkdir(parents=True, exist_ok=True)
                shared_fresh = self._is_fresh(shared, require_existing)
                self.db.execute("ATTACH DATABASE ? AS shared", (str(shared),))
                self.db.execute("PRAGMA shared.journal_mode=DELETE")
                self._initialize(
                    "shared",
                    policy.shared_max_image_calls,
                    policy.shared_max_vision_calls,
                    shared_fresh,
                )
                self._marker(shared).touch(exist_ok=True)
        except (sqlite3.Error, OSError):
            self.db.close()
            raise ConfigurationError("persisted budget ledger is corrupt or unavailable") from None
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def _marker(path):
        return path.with_name(path.name + ".initialized")

    @classmethod
    def _is_fresh(cls, path, require_existing):
        if path is None:
            return True
        if not path.is_file():
            if require_existing or cls._marker(path).exists():
                raise ConfigurationError("persisted budget ledger missing; refusing to reset usage")
            return True
        if path.stat().st_size == 0:
            raise ConfigurationError("persisted budget ledger empty; refusing to reset usage")
        return False

    def _initialize(self, schema, image, vision, fresh):
        if fresh:
            self.db.execute(
                f"CREATE TABLE {schema}.budget (kind TEXT PRIMARY KEY, used INTEGER NOT NULL, ceiling INTEGER NOT NULL)"
            )
            self.db.execute(
                f"CREATE TABLE {schema}.reservations (kind TEXT NOT NULL, operation TEXT)"
            )
            self.db.executemany(
                f"INSERT INTO {schema}.budget VALUES (?, 0, ?)",
                [("image", image), ("vision", vision)],
            )
            self.db.commit()
        expected = {"image": image, "vision": vision}
        rows = self.db.execute(f"SELECT kind, used, ceiling FROM {schema}.budget").fetchall()
        if len(rows) != 2 or {row[0] for row in rows} != set(expected):
            raise ConfigurationError("persisted budget rows missing or corrupt")
        for kind, used, ceiling in rows:
            if ceiling != expected[kind]:
                raise ConfigurationError("persisted budget limits cannot be changed on resume")
            reserved = self.db.execute(
                f"SELECT COUNT(*) FROM {schema}.reservations WHERE kind=?", (kind,)
            ).fetchone()[0]
            if not isinstance(used, int) or used < 0 or used > ceiling or reserved != used:
                raise ConfigurationError("persisted budget counter integrity validation failed")
        invalid = self.db.execute(
            f"SELECT COUNT(*) FROM {schema}.reservations WHERE kind NOT IN ('image', 'vision')"
        ).fetchone()[0]
        if invalid:
            raise ConfigurationError("persisted budget reservation integrity validation failed")

    def reserve(self, kind, operation=None):
        schemas = ["main", "shared"] if self.policy.shared_ledger_path else ["main"]
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                for schema in schemas:
                    used, limit = self.db.execute(
                        f"SELECT used, ceiling FROM {schema}.budget WHERE kind=?", (kind,)
                    ).fetchone()
                    if used >= limit:
                        raise AgentError(
                            f"{kind} call budget exhausted",
                            code="budget_exhausted",
                            kind="capability",
                        )
                for schema in schemas:
                    self.db.execute(f"UPDATE {schema}.budget SET used=used+1 WHERE kind=?", (kind,))
                    self.db.execute(
                        f"INSERT INTO {schema}.reservations VALUES (?, ?)", (kind, operation)
                    )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def image_operations(self):
        return dict(
            self.db.execute(
                "SELECT operation, COUNT(*) FROM reservations WHERE kind='image' GROUP BY operation"
            )
        )

    def usage(self):
        return {
            kind + "_calls": used for kind, used in self.db.execute("SELECT kind, used FROM budget")
        }

    def close(self):
        self.db.close()


_current_call = ContextVar("image_agent_call", default=None)


@contextmanager
def call_scope(ledger, kind, journal=None, operation=None):
    token = _current_call.set((ledger, kind, journal, operation))
    try:
        yield
    finally:
        _current_call.reset(token)


def reserve_post():
    scope = _current_call.get()
    if scope:
        ledger, kind, journal, operation = scope
        ledger.reserve(kind, operation)
        if journal is not None and operation is not None:
            journal.mark_operation(operation, "submitted")
    return scope[1] if scope else None
