import re
from pathlib import Path

import pytest

from langfuse_to_snowflake.entities import ENTITIES, Column, load_table_name, table_name
from langfuse_to_snowflake.snowflake import sql


def test_column_expr_casts_by_type():
    assert sql.column_expr(Column("NAME", "STRING", ("name",))) == 'RAW:"name"::STRING'
    assert (
        sql.column_expr(Column("START_TIME", "TIMESTAMP_TZ", ("startTime",)))
        == 'TRY_TO_TIMESTAMP_TZ(RAW:"startTime"::STRING)'
    )
    assert sql.column_expr(Column("TAGS", "VARIANT", ("tags",))) == 'STRIP_NULL_VALUE(RAW:"tags")'


def test_column_expr_coalesces_candidate_paths_including_nested_ones():
    column = Column("TOTAL_COST", "FLOAT", ("totalCost", "costDetails.total"))

    assert sql.column_expr(column) == (
        'COALESCE(TRY_TO_DOUBLE(RAW:"totalCost"::STRING), '
        'TRY_TO_DOUBLE(RAW:"costDetails"."total"::STRING))'
    )


def test_column_expr_fills_in_custom_expressions():
    column = Column("X", "STRING", expr='UPPER({raw}:"x"::STRING)')

    assert sql.column_expr(column, raw="SRC") == 'UPPER(SRC:"x"::STRING)'


@pytest.mark.parametrize("name", list(ENTITIES))
def test_every_entity_produces_consistent_ddl_and_merge(name):
    spec = ENTITIES[name]
    table = table_name("LANGFUSE_", name)
    ddl = sql.create_table(table, spec)
    merge = sql.merge(table, load_table_name("LANGFUSE_", name), spec)

    assert ddl.startswith(f"CREATE TABLE IF NOT EXISTS LANGFUSE_{name.upper()} (")
    for column in spec.columns:
        assert f'"{column.name}" {column.type}' in ddl
        assert f'AS "{column.name}"' in merge
    assert '"RAW" VARIANT NOT NULL' in ddl
    # Custom expressions must have had their placeholder filled in.
    assert "{raw}" not in merge
    # The key and dedupe columns must exist on the table.
    assert set(spec.key) <= {column.name for column in spec.columns}
    assert spec.dedupe_order in {column.name for column in spec.columns}
    # pyformat binding: the project id is the only "%" in the statement.
    assert merge.count("%") == 1 and "%(project_id)s" in merge


def test_merge_keys_observations_on_trace_and_id():
    spec = ENTITIES["observations"]
    merge = sql.merge("LANGFUSE_OBSERVATIONS", "LANGFUSE_OBSERVATIONS__LOAD", spec)

    assert (
        'ON t."PROJECT_ID" = s."PROJECT_ID" AND EQUAL_NULL(t."TRACE_ID", s."TRACE_ID") '
        'AND t."ID" = s."ID"'
    ) in merge
    assert 'PARTITION BY "TRACE_ID", "ID" ORDER BY "UPDATED_AT" DESC NULLS LAST' in merge
    assert 'WHEN MATCHED AND t."RAW" <> s."RAW" THEN UPDATE SET' in merge
    # Key columns are never rewritten.
    update = merge.split("THEN UPDATE SET")[1].split("WHEN NOT MATCHED")[0]
    assert '"TRACE_ID" =' not in update and '"ID" =' not in update
    assert '"_LOADED_AT" = CURRENT_TIMESTAMP()' in update


def test_merge_insert_lists_match():
    merge = sql.merge("LANGFUSE_SESSIONS", "LANGFUSE_SESSIONS__LOAD", ENTITIES["sessions"])
    insert = merge.split("WHEN NOT MATCHED THEN INSERT ")[1]
    columns, values = insert.split("VALUES")

    assert (
        columns.strip() == '("PROJECT_ID", "ID", "CREATED_AT", "ENVIRONMENT", "RAW", "_LOADED_AT")'
    )
    assert values.strip() == (
        '(s."PROJECT_ID", s."ID", s."CREATED_AT", s."ENVIRONMENT", s."RAW", CURRENT_TIMESTAMP())'
    )


def test_put_uses_a_forward_slash_file_uri(tmp_path: Path):
    statement = sql.put(tmp_path / "scores_abc.ndjson.gz", "LANGFUSE_SCORES__LOAD")

    assert statement.startswith("PUT 'file://")
    assert "\\" not in statement
    assert "scores_abc.ndjson.gz' @%LANGFUSE_SCORES__LOAD " in statement
    assert "SOURCE_COMPRESSION = GZIP" in statement


def test_copy_targets_one_file_and_continues_past_bad_rows():
    statement = sql.copy("LANGFUSE_SCORES__LOAD", "scores_abc.ndjson.gz")

    assert "FROM @%LANGFUSE_SCORES__LOAD FILES = ('scores_abc.ndjson.gz')" in statement
    assert "ON_ERROR = CONTINUE" in statement and "PURGE = TRUE" in statement


@pytest.mark.parametrize("name", list(ENTITIES))
def test_key_and_earliest_queries_use_columns_the_table_has(name):
    spec = ENTITIES[name]
    columns = {column.name for column in spec.columns}
    table = table_name("LANGFUSE_", name)

    keys = sql.select_keys(table, spec)
    earliest = sql.select_earliest(table, spec)

    assert spec.time_column in columns
    assert len(spec.key) == len(spec.key_paths)
    for statement in (keys, earliest):
        assert f"FROM {table} " in statement
        # Every quoted name is a real column, or the alias of the computed one.
        assert set(re.findall(r'"([A-Z_]+)"', statement)) <= columns | {
            "PROJECT_ID",
            "UPDATED_AT",
            "EARLIEST",
        }
    assert keys.startswith("SELECT " + ", ".join(f'"{key}"' for key in spec.key))


def test_state_statements_cover_reconciliation():
    assert '"RECONCILED_AT" TIMESTAMP_TZ' in sql.create_state_table("LANGFUSE_SYNC_STATE")
    assert '"RECONCILED_AT"' in sql.select_state("LANGFUSE_SYNC_STATE")
    assert sql.update_reconciled("LANGFUSE_SYNC_STATE") == (
        'UPDATE LANGFUSE_SYNC_STATE SET "RECONCILED_AT" = %(reconciled_at)s::TIMESTAMP_TZ '
        'WHERE "PROJECT_ID" = %(project_id)s AND "ENTITY" = %(entity)s'
    )


@pytest.mark.parametrize("name", ["traces", "sessions"])
def test_views_only_read_columns_the_observations_table_has(name):
    statement = sql.create_view(name, f"LANGFUSE_{name.upper()}", "LANGFUSE_OBSERVATIONS")

    assert statement.startswith(f"CREATE OR REPLACE VIEW LANGFUSE_{name.upper()} COPY GRANTS AS")
    assert "FROM LANGFUSE_OBSERVATIONS" in statement
    assert "{" not in statement

    available = {column.name for column in ENTITIES["observations"].columns}
    available |= {"PROJECT_ID", "RAW", "_LOADED_AT"}
    referenced = set(re.findall(r'"([A-Z_]+)"', statement))
    produced = set(re.findall(r'AS "([A-Z_]+)"', statement))
    assert referenced - produced <= available
