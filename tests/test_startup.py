"""Snowpark takes seconds to import. Commands that do not reach Snowflake must not pay for it."""

import subprocess
import sys

import pytest

CHECK = """
import sys
{statement}
loaded = sorted(name for name in sys.modules if name.startswith(("snowflake.snowpark", "pandas")))
print(",".join(loaded))
"""


def _loaded_by(statement: str) -> list[str]:
    output = subprocess.run(
        [sys.executable, "-c", CHECK.format(statement=statement)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return [name for name in output.split(",") if name]


@pytest.mark.parametrize(
    "statement",
    [
        "import langfuse_to_snowflake.cli",
        "import langfuse_to_snowflake.api",
        "from langfuse_to_snowflake.snowflake import Sessions, LoadResult, Catalog",
        "from langfuse_to_snowflake.sync import SyncService, open_service, open_runtime",
    ],
)
def test_importing_the_package_does_not_load_snowpark(statement):
    assert _loaded_by(statement) == []


def test_the_adapter_loads_snowpark_when_it_is_asked_for():
    loaded = _loaded_by("from langfuse_to_snowflake.snowflake import SnowflakeAdapter")

    assert "snowflake.snowpark" in loaded
