"""In-memory stand-ins for Langfuse and Snowflake."""

import gzip
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from langfuse_to_snowflake.snowflake import EntityState, LoadResult


def parse_time(value) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection
        self._rows: list[tuple] = []
        self.description = None

    def execute(self, statement: str, params=None) -> None:
        self._connection.statements.append((statement, params))
        self._connection.raise_if_scheduled(statement)
        columns, self._rows = self._connection.respond(statement, params)
        self.description = [(name,) for name in columns] if columns else None

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        pass


class FakeConnection:
    """Records every statement and answers the ones the adapter reads results from."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict | None]] = []
        self.staged: dict[str, list[dict]] = {}
        self.object_types: dict[str, str] = {}
        self.state: dict[tuple[str, str], dict] = {}
        self.reconciled: dict[tuple[str, str], str] = {}
        # Canned answers for the key and earliest-timestamp queries, by table name.
        self.keys: dict[str, list[tuple]] = {}
        self.earliest: dict[str, datetime] = {}
        self.reject_per_file = 0
        self.closed = False
        self._copied = 0
        self._failures: list[tuple[str, Exception]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    def fail_next(self, prefix: str, error: Exception) -> None:
        """Make the next statement starting with ``prefix`` raise ``error``, once."""
        self._failures.append((prefix, error))

    def raise_if_scheduled(self, statement: str) -> None:
        for index, (prefix, error) in enumerate(self._failures):
            if statement.startswith(prefix):
                del self._failures[index]
                raise error

    def executed(self, prefix: str) -> list[str]:
        return [statement for statement, _ in self.statements if statement.startswith(prefix)]

    def respond(self, statement: str, params) -> tuple[list[str], list[tuple]]:
        if statement.startswith("SELECT CURRENT_ACCOUNT"):
            return (
                ["account", "user", "role", "warehouse", "database", "schema"],
                [("acct", "loader", "LOADER", "WH", "DB", "LANGFUSE")],
            )
        if statement.startswith("PUT"):
            path = Path(re.search(r"'file://(.+?)'", statement).group(1))
            with gzip.open(path, "rt", encoding="ascii") as handle:
                self.staged[path.name] = [json.loads(line) for line in handle]
            return ["source", "status"], [(path.name, "UPLOADED")]
        if statement.startswith("COPY INTO"):
            filename = re.search(r"FILES = \('(.+?)'\)", statement).group(1)
            parsed = len(self.staged[filename])
            rejected = min(self.reject_per_file, parsed)
            self._copied = parsed - rejected
            return (
                ["file", "status", "rows_parsed", "rows_loaded", "errors_seen", "first_error"],
                [
                    (
                        filename,
                        "PARTIALLY_LOADED" if rejected else "LOADED",
                        parsed,
                        parsed - rejected,
                        rejected,
                        "Error parsing JSON" if rejected else None,
                    )
                ],
            )
        if statement.startswith("MERGE INTO") and "%(watermark)s" in statement:
            self.state[(params["project_id"], params["entity"])] = dict(params)
            return ["number of rows inserted", "number of rows updated"], [(1, 0)]
        if statement.startswith("MERGE INTO"):
            return ["number of rows inserted", "number of rows updated"], [(self._copied, 0)]
        if statement.startswith("UPDATE") and '"RECONCILED_AT"' in statement:
            key = (params["project_id"], params["entity"])
            if key in self.state:
                self.reconciled[key] = params["reconciled_at"]
            return ["number of rows updated"], [(int(key in self.state),)]
        if "INFORMATION_SCHEMA.TABLES" in statement:
            kind = self.object_types.get(params["name"])
            return ["TABLE_TYPE"], [(kind,)] if kind else []
        if statement.startswith('SELECT "ENTITY"'):
            now = datetime(2026, 1, 1, tzinfo=UTC)
            rows = [
                (
                    entity,
                    datetime.fromisoformat(saved["watermark"]),
                    saved["api_version"],
                    now,
                    parse_time(self.reconciled.get((project_id, entity))),
                )
                for (project_id, entity), saved in sorted(self.state.items())
                if project_id == params["project_id"]
            ]
            columns = ["ENTITY", "WATERMARK", "API_VERSION", "UPDATED_AT", "RECONCILED_AT"]
            return columns, rows
        if statement.startswith("SELECT MIN("):
            table = re.search(r"FROM (\w+)", statement).group(1)
            return ["EARLIEST"], [(self.earliest.get(table),)]
        if statement.startswith("SELECT") and '"UPDATED_AT" FROM' in statement:
            table = re.search(r"FROM (\w+)", statement).group(1)
            columns = re.findall(r'"([A-Z_]+)"', statement.split(" FROM ")[0])
            return columns, self.keys.get(table, [])
        return [], []


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
