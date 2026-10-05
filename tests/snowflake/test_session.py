import threading

import pytest
from snowflake.connector import errors

from langfuse_to_snowflake.snowflake import Catalog, Sessions, SnowflakeAdapter
from langfuse_to_snowflake.snowflake import session as session_module


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.statements: list[str] = []
        self.rows: list[dict] = []

    def close(self) -> None:
        self.closed = True

    def sql(self, statement: str) -> "FakeSession":
        self.statements.append(statement)
        return self

    # Enough of a DataFrame for the catalog's one query.
    def table(self, name: str) -> "FakeSession":
        self.statements.append(f"table {name}")
        return self

    def filter(self, condition) -> "FakeSession":
        return self

    def select(self, *columns) -> "FakeSession":
        return self

    def collect(self) -> list[dict]:
        return self.rows

    def get_current_account(self) -> str:
        return '"ACCT"'

    get_current_user = get_current_role = get_current_warehouse = get_current_account
    get_current_database = get_current_schema = get_current_account


class FakeBuilder:
    """Stands in for ``Session.builder``: remembers what it was asked to connect with."""

    def __init__(self) -> None:
        self.failures: list[Exception] = []
        self.attempts: list[dict] = []
        self.opened: list[FakeSession] = []

    def configs(self, options: dict) -> "FakeBuilder":
        self.attempts.append(options)
        return self

    def create(self) -> FakeSession:
        if self.failures:
            raise self.failures.pop(0)
        self.opened.append(FakeSession())
        return self.opened[-1]


@pytest.fixture
def builder(monkeypatch) -> FakeBuilder:
    from snowflake.snowpark import Session

    builder = FakeBuilder()
    monkeypatch.setattr(Session, "builder", builder)
    monkeypatch.setattr(session_module, "load_private_key", lambda settings: b"der")
    return builder


def test_a_session_is_opened_with_the_key_pair_and_the_configured_context(
    snowflake_settings, builder
):
    settings = snowflake_settings.model_copy(update={"role": "LOADER"})

    with Sessions(settings).use():
        pass

    (options,) = builder.attempts
    assert options["private_key"] == b"der"
    assert (options["account"], options["user"], options["role"]) == ("acct", "loader", "LOADER")
    assert (options["warehouse"], options["database"], options["schema"]) == (
        "WH",
        "DB",
        "LANGFUSE",
    )
    assert options["validate_default_parameters"] is True
    assert options["session_parameters"]["TIMEZONE"] == "UTC"


def test_no_role_is_sent_when_none_is_configured(snowflake_settings, builder):
    with Sessions(snowflake_settings).use():
        pass

    assert "role" not in builder.attempts[0]


def test_nested_uses_share_one_session_and_the_outermost_closes_it(snowflake_settings, builder):
    sessions = Sessions(snowflake_settings)

    with sessions.use() as outer:
        with sessions.use() as inner:
            assert inner is outer
        assert not outer.closed
    assert outer.closed
    assert len(builder.opened) == 1

    with sessions.use() as again:
        assert again is not outer
    assert len(builder.opened) == 2


def test_the_session_is_closed_when_the_block_fails(snowflake_settings, builder):
    sessions = Sessions(snowflake_settings)

    with pytest.raises(ValueError, match="boom"), sessions.use():
        raise ValueError("boom")

    assert builder.opened[0].closed


def test_each_thread_gets_a_session_of_its_own(snowflake_settings, builder):
    sessions = Sessions(snowflake_settings)
    seen = []

    def work() -> None:
        with sessions.use() as session:
            seen.append(session)

    with sessions.use() as mine:
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()

    assert seen[0] is not mine
    assert seen[0].closed


def test_connecting_is_retried(snowflake_settings, builder, caplog):
    builder.failures.append(errors.OperationalError(msg="Service temporarily unavailable"))
    sleeps: list[float] = []
    adapter = SnowflakeAdapter(snowflake_settings, sleep=sleeps.append)

    with caplog.at_level("WARNING"), adapter:
        assert adapter.ping()["account"] == "ACCT"

    assert len(builder.attempts) == 2 and len(sleeps) == 1
    assert "Snowflake operation failed" in caplog.text
    assert builder.opened[0].closed


def test_a_session_handed_in_is_used_and_never_closed():
    given = FakeSession()
    sessions = Sessions(session=given)

    with sessions.use() as session:
        assert session is given

    assert not given.closed


def test_sessions_need_something_to_connect_with():
    with pytest.raises(ValueError, match="either settings"):
        Sessions()


def test_the_catalog_tells_tables_from_views():
    session = FakeSession()
    session.rows = [
        {"TABLE_NAME": "LANGFUSE_SCORES", "TABLE_TYPE": "BASE TABLE"},
        {"TABLE_NAME": "LANGFUSE_TRACES", "TABLE_TYPE": "VIEW"},
    ]

    kinds = Catalog(session).kinds(["LANGFUSE_SCORES", "LANGFUSE_TRACES", "LANGFUSE_SESSIONS"])

    assert kinds == {"LANGFUSE_SCORES": "TABLE", "LANGFUSE_TRACES": "VIEW"}
    assert session.statements == ["table INFORMATION_SCHEMA.TABLES"]


def test_the_catalog_adds_one_column_per_statement():
    session = FakeSession()

    Catalog(session).add_columns(
        "LANGFUSE_SCORES", [("COMMENT", "STRING"), ("PROMPT_VERSION", "NUMBER")]
    )

    assert session.statements == [
        'ALTER TABLE LANGFUSE_SCORES ADD COLUMN IF NOT EXISTS "COMMENT" STRING',
        'ALTER TABLE LANGFUSE_SCORES ADD COLUMN IF NOT EXISTS "PROMPT_VERSION" NUMBER(38, 0)',
    ]


def test_the_catalog_runs_a_view_definition_as_given():
    session = FakeSession()

    Catalog(session).create_view("V", "CREATE OR REPLACE VIEW V AS SELECT 1")

    assert session.statements == ["CREATE OR REPLACE VIEW V AS SELECT 1"]
