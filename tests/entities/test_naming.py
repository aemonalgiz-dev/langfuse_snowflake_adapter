import pytest

from langfuse_to_snowflake.entities import (
    runs_table_name,
    settings_table_name,
    state_table_name,
    table_name,
    validate_prefix,
)


def test_prefix_validation():
    assert validate_prefix("lf_") == "LF_"
    assert validate_prefix("") == ""
    for bad in ('LF"; DROP TABLE X; --', "1LF", "LF-", "LF "):
        with pytest.raises(ValueError):
            validate_prefix(bad)


def test_object_names():
    assert table_name("LF_", "traces") == "LF_TRACES"
    assert state_table_name("LF_") == "LF_SYNC_STATE"
    assert settings_table_name("lf_") == "LF_SYNC_SETTINGS"
    assert runs_table_name("") == "SYNC_RUNS"
