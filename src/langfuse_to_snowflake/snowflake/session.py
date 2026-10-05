"""Opening Snowpark sessions, and the little that takes SQL rather than a DataFrame."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

from ..config import SnowflakeSettings
from .auth import load_private_key
from .retry import build_retrying

if TYPE_CHECKING:
    from snowflake.snowpark import Session

ObjectKind = Literal["TABLE", "VIEW"]
# A column to add: its name and its Snowflake type.
NewColumn = tuple[str, str]

_SQL_TYPES = {"NUMBER": "NUMBER(38, 0)"}


class Catalog:
    """The part of schema management that is written as SQL.

    Snowpark has no DataFrame call for telling a table from a view or for
    adding a column, and the views are defined in SQL. Its local emulator runs
    no SQL at all, so the tests put a stand-in here; everything else they run
    for real.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def kinds(self, names: Iterable[str]) -> dict[str, ObjectKind]:
        """Whether each of ``names`` is a table or a view. Names that do not exist are left out."""
        from snowflake.snowpark import functions as F

        tables = self._session.table("INFORMATION_SCHEMA.TABLES")
        rows = (
            tables.filter(
                (F.col("TABLE_SCHEMA") == F.current_schema())
                & F.col("TABLE_NAME").isin(list(names))
            )
            .select("TABLE_NAME", "TABLE_TYPE")
            .collect()
        )
        return {
            row["TABLE_NAME"]: "VIEW" if row["TABLE_TYPE"] == "VIEW" else "TABLE" for row in rows
        }

    def add_columns(self, table: str, columns: Sequence[NewColumn]) -> None:
        # Table and column names come from validated prefixes and fixed
        # definitions, never from data.
        for name, kind in columns:
            sql_type = _SQL_TYPES.get(kind, kind)
            self._session.sql(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{name}" {sql_type}'
            ).collect()

    def create_view(self, name: str, statement: str) -> None:
        self._session.sql(statement).collect()


class Sessions:
    """Opens sessions on one Snowflake account, with key-pair authentication.

    ``use`` nests: on one thread, an inner block gets the session an outer
    block opened. Everything one run does can so share a single login.
    """

    def __init__(
        self,
        settings: SnowflakeSettings | None = None,
        *,
        session: Session | None = None,
        catalog: Callable[[Session], Any] = Catalog,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings is None and session is None:
            raise ValueError("Sessions needs either settings to connect with or a session")
        self._settings = settings
        # A session handed in is used as it is and never closed.
        self._given = session
        self._catalog = catalog
        self._retrying = build_retrying(settings.max_retries if settings else 0, sleep)
        self._local = threading.local()

    @property
    def retrying(self) -> Any:
        return self._retrying

    def catalog(self, session: Session) -> Any:
        return self._catalog(session)

    @contextmanager
    def use(self) -> Iterator[Session]:
        if self._given is not None:
            yield self._given
            return
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._local.session = self._retrying(self._open)
        self._local.depth = depth + 1
        try:
            yield self._local.session
        finally:
            self._local.depth -= 1
            if self._local.depth == 0:
                session, self._local.session = self._local.session, None
                session.close()

    def _open(self) -> Session:
        from snowflake.snowpark import Session

        settings = self._settings
        assert settings is not None
        options: dict[str, Any] = {
            "account": settings.account,
            "user": settings.user,
            "private_key": load_private_key(settings),
            "warehouse": settings.warehouse,
            "database": settings.database,
            "schema": settings.schema_name,
            "application": "langfuse_to_snowflake",
            "session_parameters": {"QUERY_TAG": "langfuse_to_snowflake", "TIMEZONE": "UTC"},
            # Fail at connect time if the database, schema or warehouse is missing.
            "validate_default_parameters": True,
            # Seconds. An account that cannot be reached is reported, not waited for.
            "login_timeout": 60,
        }
        if settings.role:
            options["role"] = settings.role
        return Session.builder.configs(options).create()
