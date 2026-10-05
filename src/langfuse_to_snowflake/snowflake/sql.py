"""SQL builders for the Snowflake adapter.

Table names come from a validated prefix plus a fixed entity name and are left
unquoted; column names are always double-quoted so that names such as COMMENT
or TIMESTAMP never collide with keywords. Values are passed as bind parameters.
"""

from __future__ import annotations

from pathlib import Path

from ..entities import Column, EntitySpec

RAW = "RAW"

_CASTS = {
    "STRING": "{0}::STRING",
    "TIMESTAMP_TZ": "TRY_TO_TIMESTAMP_TZ({0}::STRING)",
    "FLOAT": "TRY_TO_DOUBLE({0}::STRING)",
    "NUMBER": "TRY_TO_NUMBER({0}::STRING)",
    "BOOLEAN": "TRY_TO_BOOLEAN({0}::STRING)",
    # A JSON null would otherwise be stored as a VARIANT null rather than SQL NULL.
    "VARIANT": "STRIP_NULL_VALUE({0})",
}


def quote(name: str) -> str:
    return f'"{name}"'


def json_path(path: str, raw: str = RAW) -> str:
    """``usage.input`` -> ``RAW:"usage"."input"``"""
    head, *rest = path.split(".")
    return f'{raw}:"{head}"' + "".join(f'."{part}"' for part in rest)


def column_expr(column: Column, raw: str = RAW) -> str:
    if column.expr is not None:
        return column.expr.format(raw=raw)
    cast = _CASTS[column.type]
    candidates = [cast.format(json_path(path, raw)) for path in column.paths]
    if len(candidates) == 1:
        return candidates[0]
    return f"COALESCE({', '.join(candidates)})"


def create_table(table: str, spec: EntitySpec) -> str:
    columns = ['"PROJECT_ID" STRING NOT NULL']
    for column in spec.columns:
        not_null = " NOT NULL" if column.name == "ID" else ""
        columns.append(f"{quote(column.name)} {column.type}{not_null}")
    columns += ['"RAW" VARIANT NOT NULL', '"_LOADED_AT" TIMESTAMP_TZ NOT NULL']
    body = ",\n    ".join(columns)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {body}\n)"


def create_load_table(load_table: str) -> str:
    return f'CREATE TEMPORARY TABLE IF NOT EXISTS {load_table} ("RAW" VARIANT)'


def truncate(table: str) -> str:
    return f"TRUNCATE TABLE {table}"


def put(path: Path, load_table: str) -> str:
    """Upload an already-gzipped file to the load table's own stage."""
    uri = "file://" + path.resolve().as_posix().replace("'", "\\'")
    return (
        f"PUT '{uri}' @%{load_table} "
        "AUTO_COMPRESS = FALSE SOURCE_COMPRESSION = GZIP OVERWRITE = TRUE"
    )


def copy(load_table: str, filename: str) -> str:
    # ON_ERROR = CONTINUE: one unparseable or oversized record must not block the
    # whole sync. The adapter reports skipped rows from the COPY result.
    return (
        f"COPY INTO {load_table} FROM @%{load_table} FILES = ('{filename}') "
        "FILE_FORMAT = (TYPE = JSON) ON_ERROR = CONTINUE PURGE = TRUE"
    )


def merge(table: str, load_table: str, spec: EntitySpec) -> str:
    """Upsert the load table into the target, keyed on PROJECT_ID plus the entity key.

    Rows whose raw record is unchanged are left alone, so _LOADED_AT only moves
    when a record actually changed and re-reading the lookback window is cheap.
    """
    data_columns = [column.name for column in spec.columns] + ["RAW"]
    projected = ",\n".join(
        f"            {column_expr(column)} AS {quote(column.name)}" for column in spec.columns
    )
    partition = ", ".join(quote(name) for name in spec.key)
    on = ['t."PROJECT_ID" = s."PROJECT_ID"']
    for name in spec.key:
        column = quote(name)
        # Only ID is guaranteed non-null (legacy observations may lack a trace).
        on.append(
            f"t.{column} = s.{column}" if name == "ID" else f"EQUAL_NULL(t.{column}, s.{column})"
        )
    updates = [f"{quote(name)} = s.{quote(name)}" for name in data_columns if name not in spec.key]
    updates.append('"_LOADED_AT" = CURRENT_TIMESTAMP()')
    insert_columns = ["PROJECT_ID", *data_columns, "_LOADED_AT"]
    insert_values = ['s."PROJECT_ID"', *(f"s.{quote(name)}" for name in data_columns)]
    insert_values.append("CURRENT_TIMESTAMP()")

    return f"""MERGE INTO {table} AS t
USING (
    SELECT * FROM (
        SELECT
            %(project_id)s AS "PROJECT_ID",
{projected},
            {RAW} AS "RAW"
        FROM {load_table}
    )
    WHERE "ID" IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY {partition} ORDER BY {quote(spec.dedupe_order)} DESC NULLS LAST
    ) = 1
) AS s
ON {" AND ".join(on)}
WHEN MATCHED AND t."RAW" <> s."RAW" THEN UPDATE SET
    {", ".join(updates)}
WHEN NOT MATCHED THEN INSERT ({", ".join(quote(name) for name in insert_columns)})
    VALUES ({", ".join(insert_values)})"""


def create_state_table(table: str) -> str:
    return f"""CREATE TABLE IF NOT EXISTS {table} (
    "PROJECT_ID" STRING NOT NULL,
    "ENTITY" STRING NOT NULL,
    "WATERMARK" TIMESTAMP_TZ NOT NULL,
    "API_VERSION" STRING,
    "UPDATED_AT" TIMESTAMP_TZ NOT NULL,
    "RECONCILED_AT" TIMESTAMP_TZ
)"""


def add_reconciled_column(table: str) -> str:
    """For state tables created before reconciliation existed."""
    return f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "RECONCILED_AT" TIMESTAMP_TZ'


def select_state(table: str) -> str:
    return (
        'SELECT "ENTITY", "WATERMARK", "API_VERSION", "UPDATED_AT", "RECONCILED_AT" '
        f'FROM {table} WHERE "PROJECT_ID" = %(project_id)s ORDER BY "ENTITY"'
    )


def update_reconciled(table: str) -> str:
    return (
        f'UPDATE {table} SET "RECONCILED_AT" = %(reconciled_at)s::TIMESTAMP_TZ '
        'WHERE "PROJECT_ID" = %(project_id)s AND "ENTITY" = %(entity)s'
    )


def select_keys(table: str, spec: EntitySpec, *, ranged: bool = True) -> str:
    """The keys held, with the updatedAt each row was loaded at.

    ``ranged`` limits them to a time range; without it, every key of the project.
    """
    has_updated_at = any(column.name == "UPDATED_AT" for column in spec.columns)
    columns = [quote(name) for name in spec.key]
    columns.append('"UPDATED_AT"' if has_updated_at else 'NULL AS "UPDATED_AT"')
    statement = f'SELECT {", ".join(columns)} FROM {table} WHERE "PROJECT_ID" = %(project_id)s'
    if ranged:
        time = quote(spec.time_column)
        statement += f" AND {time} >= %(start)s::TIMESTAMP_TZ AND {time} < %(end)s::TIMESTAMP_TZ"
    return statement


def select_earliest(table: str, spec: EntitySpec) -> str:
    return (
        f'SELECT MIN({quote(spec.time_column)}) AS "EARLIEST" FROM {table} '
        'WHERE "PROJECT_ID" = %(project_id)s'
    )


def upsert_state(table: str) -> str:
    return f"""MERGE INTO {table} AS t
USING (
    SELECT
        %(project_id)s AS "PROJECT_ID",
        %(entity)s AS "ENTITY",
        %(watermark)s::TIMESTAMP_TZ AS "WATERMARK",
        %(api_version)s AS "API_VERSION"
) AS s
ON t."PROJECT_ID" = s."PROJECT_ID" AND t."ENTITY" = s."ENTITY"
WHEN MATCHED THEN UPDATE SET
    "WATERMARK" = s."WATERMARK", "API_VERSION" = s."API_VERSION",
    "UPDATED_AT" = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT ("PROJECT_ID", "ENTITY", "WATERMARK", "API_VERSION", "UPDATED_AT")
    VALUES (s."PROJECT_ID", s."ENTITY", s."WATERMARK", s."API_VERSION", CURRENT_TIMESTAMP())"""


def select_object_type() -> str:
    return (
        "SELECT TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = CURRENT_SCHEMA() AND TABLE_NAME = %(name)s"
    )


# Langfuse v4 has no trace or session objects; these views rebuild them by
# grouping observations, as the Langfuse migration guide prescribes.
_VIEWS = {
    "traces": """CREATE OR REPLACE VIEW {view} COPY GRANTS AS
SELECT
    "PROJECT_ID",
    "TRACE_ID" AS "ID",
    COALESCE(MAX("TRACE_NAME"), MAX(IFF("IS_ROOT_OBSERVATION", "NAME", NULL))) AS "NAME",
    MIN("START_TIME") AS "TIMESTAMP",
    MAX(COALESCE("END_TIME", "START_TIME")) AS "END_TIME",
    MAX("USER_ID") AS "USER_ID",
    MAX("SESSION_ID") AS "SESSION_ID",
    MAX("ENVIRONMENT") AS "ENVIRONMENT",
    MAX("RELEASE") AS "RELEASE",
    MAX(IFF("IS_ROOT_OBSERVATION", "VERSION", NULL)) AS "VERSION",
    MAX_BY("TAGS", ARRAY_SIZE("TAGS")) AS "TAGS",
    MAX(IFF("IS_ROOT_OBSERVATION", "RAW":"input"::STRING, NULL)) AS "INPUT",
    MAX(IFF("IS_ROOT_OBSERVATION", "RAW":"output"::STRING, NULL)) AS "OUTPUT",
    COUNT(*) AS "OBSERVATION_COUNT",
    COUNT_IF("LEVEL" = 'ERROR') AS "ERROR_COUNT",
    SUM("INPUT_TOKENS") AS "INPUT_TOKENS",
    SUM("OUTPUT_TOKENS") AS "OUTPUT_TOKENS",
    SUM("TOTAL_TOKENS") AS "TOTAL_TOKENS",
    SUM("TOTAL_COST") AS "TOTAL_COST",
    DATEDIFF(
        'millisecond', MIN("START_TIME"), MAX(COALESCE("END_TIME", "START_TIME"))
    ) / 1000 AS "LATENCY",
    MAX("_LOADED_AT") AS "_LOADED_AT"
FROM {source}
WHERE "TRACE_ID" IS NOT NULL
GROUP BY "PROJECT_ID", "TRACE_ID"
""",
    "sessions": """CREATE OR REPLACE VIEW {view} COPY GRANTS AS
SELECT
    "PROJECT_ID",
    "SESSION_ID" AS "ID",
    MIN("START_TIME") AS "CREATED_AT",
    MAX(COALESCE("END_TIME", "START_TIME")) AS "LAST_ACTIVITY_AT",
    MAX("ENVIRONMENT") AS "ENVIRONMENT",
    ARRAY_AGG(DISTINCT "USER_ID") AS "USER_IDS",
    COUNT(DISTINCT "TRACE_ID") AS "TRACE_COUNT",
    COUNT(*) AS "OBSERVATION_COUNT",
    SUM("TOTAL_TOKENS") AS "TOTAL_TOKENS",
    SUM("TOTAL_COST") AS "TOTAL_COST",
    MAX("_LOADED_AT") AS "_LOADED_AT"
FROM {source}
WHERE "SESSION_ID" IS NOT NULL
GROUP BY "PROJECT_ID", "SESSION_ID"
""",
}


def create_view(name: str, view: str, source: str) -> str:
    return _VIEWS[name].format(view=view, source=source).strip()
