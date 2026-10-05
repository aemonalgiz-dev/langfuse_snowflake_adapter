"""In-memory stand-ins for Langfuse and Snowflake."""

from datetime import UTC, datetime

from langfuse_to_snowflake.snowflake import EntityState, LoadResult


def parse_time(value) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


class LocalCatalog:
    """Stands in for ``Catalog`` on Snowpark's emulator, which runs no SQL.

    Tables are looked up in the emulator itself. Views are only remembered:
    their SQL is kept in ``views`` for the tests to look at.
    """

    def __init__(self, session) -> None:
        self._session = session
        self.views: dict[str, str] = {}
        self.added: list[tuple[str, list[tuple[str, str]]]] = []

    def kinds(self, names) -> dict[str, str]:
        registry = self._session._conn.entity_registry
        found = {}
        for name in names:
            if name in self.views:
                found[name] = "VIEW"
            elif registry.is_existing_table(name):
                found[name] = "TABLE"
        return found

    def add_columns(self, table: str, columns) -> None:
        from snowflake.snowpark import functions as F

        from langfuse_to_snowflake.snowflake import frames

        self.added.append((table, list(columns)))
        frame = self._session.table(table)
        for name, kind in columns:
            frame = frame.with_column(name, F.lit(None).cast(frames.snowpark_type(kind)))
        frame.write.save_as_table(table, mode="overwrite")

    def create_view(self, name: str, statement: str) -> None:
        self.views[name] = statement


class FakeSource:
    """A Langfuse client returning canned pages per endpoint path.

    Like a real server it only returns records inside the requested time range,
    when they carry a timestamp. It ignores every other query parameter.
    """

    def __init__(self, pages: dict[str, list[list[dict]]] | None = None) -> None:
        self.pages = pages or {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.endpoints: list = []

    def get_project(self) -> dict:
        return {"id": "proj-1", "name": "demo"}

    def iter_pages(self, endpoint, start=None, end=None):
        self.calls.append((endpoint.path, start, end))
        self.endpoints.append(endpoint)
        if endpoint.path in self.errors:
            raise self.errors[endpoint.path]
        for page in self.pages.get(endpoint.path, []):
            rows = [row for row in page if _in_range(row, start, end)]
            if rows:
                yield rows


def _record_time(record: dict) -> datetime | None:
    for field in ("startTime", "timestamp", "createdAt"):
        if field in record:
            return parse_time(record[field])
    return None


def _in_range(record: dict, start: datetime | None, end: datetime | None) -> bool:
    at = _record_time(record)
    return at is None or start is None or end is None or start <= at < end


class FakeWarehouse:
    """Keeps loaded records by key, so that it can answer what it holds like a table."""

    def __init__(
        self,
        watermarks: dict[str, datetime] | None = None,
        reconciled: dict[str, datetime] | None = None,
    ) -> None:
        self.watermarks = dict(watermarks or {})
        self.reconciled = dict(reconciled or {})
        self.loaded: dict[str, list[dict]] = {}
        self.tables: dict[str, dict[tuple, dict]] = {}
        self.plan = None
        self.reject = 0

    def ping(self) -> dict:
        return {
            "account": "acct",
            "user": "loader",
            "role": "LOADER",
            "warehouse": "WH",
            "database": "DB",
            "schema": "LANGFUSE",
        }

    def ensure_schema(self, plan) -> None:
        self.plan = plan

    def get_state(self, project_id: str) -> list[EntityState]:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            EntityState(name, mark, "v4", now, self.reconciled.get(name))
            for name, mark in self.watermarks.items()
        ]

    def get_watermarks(self, project_id: str) -> dict[str, datetime]:
        return dict(self.watermarks)

    def set_watermark(self, project_id, entity, watermark, api_version) -> None:
        self.watermarks[entity] = watermark

    def set_reconciled(self, project_id, entity, reconciled_at) -> None:
        if entity in self.watermarks:
            self.reconciled[entity] = reconciled_at

    def seed(self, spec, records) -> None:
        """Put records in the table without counting them as loaded by a run."""
        table = self.tables.setdefault(spec.name, {})
        for record in records:
            table[tuple(record.get(path) for path in spec.key_paths)] = record

    def load(self, spec, project_id, records) -> LoadResult:
        rows = list(records)
        self.loaded.setdefault(spec.name, []).extend(rows)
        table = self.tables.setdefault(spec.name, {})
        inserted = updated = 0
        for row in rows:
            key = tuple(row.get(path) for path in spec.key_paths)
            if key not in table:
                inserted += 1
            elif table[key] != row:
                updated += 1
            table[key] = row
        return LoadResult(
            rows_inserted=max(inserted - self.reject, 0),
            rows_updated=updated,
            rows_rejected=self.reject,
        )

    def get_keys(self, spec, project_id, start=None, end=None) -> dict:
        def held(record: dict) -> bool:
            if start is None or end is None:
                return True
            at = parse_time(record.get(spec.time_path))
            return at is not None and start <= at < end

        return {
            key: parse_time(record.get("updatedAt"))
            for key, record in self.tables.get(spec.name, {}).items()
            if held(record)
        }

    def earliest(self, spec, project_id) -> datetime | None:
        times = [
            at
            for record in self.tables.get(spec.name, {}).values()
            if (at := parse_time(record.get(spec.time_path))) is not None
        ]
        return min(times, default=None)
