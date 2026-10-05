"""The columns of every table this package keeps, as name and type.

Column types are named as Snowflake names them: STRING, TIMESTAMP_TZ, FLOAT,
NUMBER, BOOLEAN and VARIANT. No column is declared NOT NULL: Snowpark creates
a table from the shape of a DataFrame, which carries no such constraint. The
loader itself never writes a row without its project, ID and record.
"""

from __future__ import annotations

from ..entities import EntitySpec

ColumnDef = tuple[str, str]

PROJECT_ID = "PROJECT_ID"
RAW = "RAW"
# A fingerprint of the record as it was loaded. Comparing it tells whether a
# record changed without comparing the records themselves, which can be large.
RAW_HASH = "_RAW_HASH"
LOADED_AT = "_LOADED_AT"
# In a batch on its way in, the record travels as JSON text.
RAW_JSON = "RAW_JSON"


def entity_columns(spec: EntitySpec) -> list[ColumnDef]:
    return [
        (PROJECT_ID, "STRING"),
        *((column.name, column.type) for column in spec.columns),
        (RAW, "VARIANT"),
        (RAW_HASH, "STRING"),
        (LOADED_AT, "TIMESTAMP_TZ"),
    ]


def batch_columns(spec: EntitySpec) -> list[ColumnDef]:
    """What ``rows.prepare`` produces, column by column."""
    return [
        (PROJECT_ID, "STRING"),
        *((column.name, column.type) for column in spec.columns),
        (RAW_JSON, "STRING"),
        (RAW_HASH, "STRING"),
    ]


STATE_COLUMNS: list[ColumnDef] = [
    (PROJECT_ID, "STRING"),
    ("ENTITY", "STRING"),
    ("WATERMARK", "TIMESTAMP_TZ"),
    ("API_VERSION", "STRING"),
    ("UPDATED_AT", "TIMESTAMP_TZ"),
    ("RECONCILED_AT", "TIMESTAMP_TZ"),
]

# One row per change to a project's settings; the newest row is in effect.
SETTINGS_COLUMNS: list[ColumnDef] = [
    ("PROJECT", "STRING"),
    ("CHANGED_AT", "TIMESTAMP_TZ"),
    ("SETTINGS", "VARIANT"),
]

# One row per run started through the service.
RUNS_COLUMNS: list[ColumnDef] = [
    ("ID", "STRING"),
    ("PROJECT", "STRING"),
    ("KIND", "STRING"),
    ("TRIGGERED_BY", "STRING"),
    ("STATUS", "STRING"),
    ("CREATED_AT", "TIMESTAMP_TZ"),
    ("STARTED_AT", "TIMESTAMP_TZ"),
    ("FINISHED_AT", "TIMESTAMP_TZ"),
    ("REQUEST", "VARIANT"),
    ("RESULT", "VARIANT"),
    ("ERROR", "STRING"),
]
