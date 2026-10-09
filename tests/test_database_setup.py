"""The marts-only rebuild and the timeframes migration in
crypto_warehouse.database_setup, against a fake connection, and the migration
file held against the schema DDL."""

from __future__ import annotations

import re

from crypto_warehouse import database_setup
from crypto_warehouse.sql_file_runner import (
    FETCH_WINDOWS_MIGRATION_FILE,
    SCHEMA_FILE,
    TIMEFRAMES_MIGRATION_FILE,
    sql_path,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.connection.log.append(" ".join(str(sql).split()))


class FakeConnection:
    """Records the statements, SQL files, and commits it sees, in order."""

    def __init__(self):
        self.log: list[str] = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.log.append("commit")

    def close(self):
        self.closed = True


def fake_dbt(monkeypatch, connection, exit_code=0):
    """Make dbt_project.build_marts record a 'dbt run' in the connection's
    log instead of running dbt, and return exit_code."""

    def build_marts(database_url=None):
        connection.log.append("dbt run")
        return exit_code

    monkeypatch.setattr(database_setup.dbt_project, "build_marts", build_marts)


def run_marts_only(monkeypatch, *, database=True, schema=True, dbt_exit_code=0):
    connection = FakeConnection()
    fake_dbt(monkeypatch, connection, dbt_exit_code)
    monkeypatch.setattr(database_setup.database_connection, "load_database_url", lambda: "postgresql://x/y")
    monkeypatch.setattr(
        database_setup.database_connection,
        "connect",
        lambda database_url=None, **kwargs: connection,
    )
    monkeypatch.setattr(database_setup, "database_exists", lambda database_url: database)
    monkeypatch.setattr(database_setup, "schema_is_applied", lambda connection: schema)
    monkeypatch.setattr(
        database_setup,
        "create_database",
        lambda database_url: connection.log.append("create_database"),
    )
    monkeypatch.setattr(
        database_setup.sql_file_runner,
        "run_sql_file",
        lambda connection, name: connection.log.append(f"file {name}"),
    )
    return database_setup.run_setup(marts_only=True), connection


def test_marts_only_drops_the_marts_schema_commits_and_then_has_dbt_build_it(monkeypatch):
    exit_code, connection = run_marts_only(monkeypatch)

    assert exit_code == 0
    assert connection.log == [
        "DROP SCHEMA IF EXISTS marts CASCADE",
        "CREATE SCHEMA marts",
        "commit",
        "dbt run",
    ]
    assert connection.closed


def test_marts_only_reports_a_failed_dbt_build(monkeypatch):
    exit_code, connection = run_marts_only(monkeypatch, dbt_exit_code=1)

    assert exit_code == 1
    assert connection.log[-1] == "dbt run"


def test_marts_only_refuses_when_the_schema_is_not_applied(monkeypatch):
    exit_code, connection = run_marts_only(monkeypatch, schema=False)

    assert exit_code == 1
    assert connection.log == []


def test_marts_only_refuses_when_the_database_is_missing_and_does_not_create_it(monkeypatch):
    exit_code, connection = run_marts_only(monkeypatch, database=False)

    assert exit_code == 1
    assert connection.log == []


# --------------------------------------------------------------------------
# The timeframes migration
# --------------------------------------------------------------------------
def run_migrate(monkeypatch, *, database=True, schema=True, pending=None):
    connection = FakeConnection()
    fake_dbt(monkeypatch, connection)
    monkeypatch.setattr(database_setup.database_connection, "load_database_url", lambda: "postgresql://x/y")
    monkeypatch.setattr(
        database_setup.database_connection,
        "connect",
        lambda database_url=None, **kwargs: connection,
    )
    monkeypatch.setattr(database_setup, "database_exists", lambda database_url: database)
    monkeypatch.setattr(database_setup, "schema_is_applied", lambda connection: schema)
    monkeypatch.setattr(
        database_setup,
        "pending_migrations",
        lambda connection: list(pending if pending is not None else [TIMEFRAMES_MIGRATION_FILE]),
    )
    monkeypatch.setattr(
        database_setup.sql_file_runner,
        "run_sql_file",
        lambda connection, name: connection.log.append(f"file {name}"),
    )
    return database_setup.run_setup(migrate=True), connection


def test_migrate_runs_both_migrations_in_order_commits_then_has_dbt_rebuild_the_marts(
    monkeypatch,
):
    exit_code, connection = run_migrate(
        monkeypatch, pending=[TIMEFRAMES_MIGRATION_FILE, FETCH_WINDOWS_MIGRATION_FILE]
    )

    assert exit_code == 0
    assert connection.log == [
        f"file {TIMEFRAMES_MIGRATION_FILE}",
        f"file {FETCH_WINDOWS_MIGRATION_FILE}",
        "commit",
        "dbt run",
    ]
    assert connection.closed


def test_migrate_leaves_the_marts_alone_when_only_the_request_log_is_pending(monkeypatch):
    exit_code, connection = run_migrate(monkeypatch, pending=[FETCH_WINDOWS_MIGRATION_FILE])

    assert exit_code == 0
    assert connection.log == [f"file {FETCH_WINDOWS_MIGRATION_FILE}", "commit"]


def test_migrate_succeeds_without_running_anything_on_a_current_warehouse(monkeypatch):
    exit_code, connection = run_migrate(monkeypatch, pending=[])

    assert exit_code == 0
    assert connection.log == []


def test_migrate_refuses_a_database_with_no_schema(monkeypatch):
    exit_code, connection = run_migrate(monkeypatch, schema=False)

    assert exit_code == 1
    assert connection.log == []


def test_migrate_refuses_when_the_database_is_missing(monkeypatch):
    exit_code, connection = run_migrate(monkeypatch, database=False)

    assert exit_code == 1
    assert connection.log == []


def create_statement(sql_text: str, table: str) -> str:
    """The CREATE TABLE statement for a table, up to its closing semicolon,
    with comments and repeated whitespace removed."""
    start = sql_text.index(f"CREATE TABLE {table} (")
    statement = sql_text[start : sql_text.index(");", start) + 2]
    statement = re.sub(r"--[^\n]*", "", statement)
    return " ".join(statement.split())


def test_the_migration_creates_the_same_tables_and_index_as_the_schema():
    schema_sql = sql_path(SCHEMA_FILE).read_text(encoding="utf-8")
    migration_sql = sql_path(TIMEFRAMES_MIGRATION_FILE).read_text(encoding="utf-8")

    for table in ("core.timeframes", "core.ohlcv"):
        assert create_statement(migration_sql, table) == create_statement(schema_sql, table)
    index = "CREATE INDEX ohlcv_granularity_ts_idx ON core.ohlcv (granularity_seconds, ts_utc);"
    assert index in schema_sql
    assert index in migration_sql
    seed = schema_sql[schema_sql.index("INSERT INTO core.timeframes") :]
    seed = seed[: seed.index(";") + 1]
    assert seed in migration_sql


def test_the_migration_keeps_every_candle_and_labels_it_hourly():
    migration_sql = sql_path(TIMEFRAMES_MIGRATION_FILE).read_text(encoding="utf-8")
    copy = " ".join(migration_sql[migration_sql.index("INSERT INTO core.ohlcv") :].split())

    assert copy.startswith(
        "INSERT INTO core.ohlcv (asset_id, granularity_seconds, ts_utc, open, high, low, close, "
        "volume, source_run_id, inserted_at, updated_at) SELECT asset_id, 3600, ts_utc, open, "
        "high, low, close, volume, source_run_id, inserted_at, updated_at FROM core.ohlcv_hourly;"
    )
    # The old table goes only after the copy.
    assert migration_sql.index("DROP TABLE core.ohlcv_hourly") > migration_sql.index(
        "INSERT INTO core.ohlcv"
    )


def test_the_request_log_migration_creates_the_same_table_and_index_as_the_schema():
    schema_sql = sql_path(SCHEMA_FILE).read_text(encoding="utf-8")
    migration_sql = sql_path(FETCH_WINDOWS_MIGRATION_FILE).read_text(encoding="utf-8")

    table = "raw.fetch_windows"
    assert create_statement(migration_sql, table) == create_statement(schema_sql, table)
    index = "CREATE INDEX fetch_windows_run_idx ON raw.fetch_windows (run_id);"
    assert index in schema_sql
    assert index in migration_sql
