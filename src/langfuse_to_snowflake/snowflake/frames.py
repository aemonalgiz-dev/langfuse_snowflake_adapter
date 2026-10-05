"""Snowpark schemas and DataFrames: the tables, and a batch on its way in."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from snowflake.snowpark import Column, DataFrame, Session
from snowflake.snowpark import functions as F
from snowflake.snowpark.types import (
    BooleanType,
    DataType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampTimeZone,
    TimestampType,
    VariantType,
)

from ..entities import EntitySpec
from .layout import RAW, RAW_JSON, ColumnDef, batch_columns
from .rows import Row

_TYPES: Mapping[str, DataType] = {
    "STRING": StringType(),
    "TIMESTAMP_TZ": TimestampType(TimestampTimeZone.TZ),
    "FLOAT": DoubleType(),
    "NUMBER": LongType(),
    "BOOLEAN": BooleanType(),
    "VARIANT": VariantType(),
}

# Snowpark writes a small frame into the statement text, and the connector
# sends a medium one inside the request. Neither suits records that can each
# be megabytes of prompt and completion: a statement may be 1 MB at most. From
# this many values on, the connector uploads the rows as a file instead, so a
# batch is filled up to it with empty rows, which the merge leaves out.
MIN_UPLOADED_VALUES = 65_280


def snowpark_type(kind: str) -> DataType:
    return _TYPES[kind]


def struct(columns: Sequence[ColumnDef]) -> StructType:
    return StructType([StructField(name, _TYPES[kind]) for name, kind in columns])


def empty(session: Session, columns: Sequence[ColumnDef]) -> DataFrame:
    """No rows, in the shape of ``columns``. Saving it creates the table."""
    return session.create_dataframe([], schema=struct(columns))


def timestamp(value: datetime) -> Column:
    """A timestamp literal that keeps its offset whatever the session is set to."""
    return F.to_timestamp_tz(F.lit(value.astimezone(UTC).isoformat()))


def batch(
    session: Session, spec: EntitySpec, rows: Sequence[Row], minimum_values: int
) -> DataFrame:
    """A batch of prepared rows as a DataFrame in the shape of the entity's table."""
    columns = batch_columns(spec)
    width = len(columns)
    filler = max(0, -(-minimum_values // width) - len(rows))
    sent = [*rows, *([(None,) * width] * filler)]
    frame = session.create_dataframe(sent, schema=struct(columns))
    selected = [
        F.parse_json(F.col(RAW_JSON)).alias(RAW) if name == RAW_JSON else F.col(name)
        for name, _ in columns
    ]
    return frame.filter(F.col("ID").is_not_null()).select(selected)


def as_utc(value: Any) -> datetime | None:
    """A timestamp read from a table, as a plain UTC datetime. None for a null."""
    # The emulator gives NaT or NaN for a null; neither equals itself.
    if value is None or value != value:  # noqa: PLR0124
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
