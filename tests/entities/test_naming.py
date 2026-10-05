import pytest

from langfuse_to_snowflake.entities import (
    load_table_name,
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
    assert load_table_name("LF_", "traces") == "LF_TRACES__LOAD"
    assert state_table_name("LF_") == "LF_SYNC_STATE"
