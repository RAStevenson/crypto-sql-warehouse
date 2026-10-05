"""The marts-only rebuild in crypto_warehouse.setup_db, against a fake connection."""

from __future__ import annotations

from crypto_warehouse import setup_db


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append(" ".join(str(sql).split()))


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


def run_marts_only(monkeypatch, *, database=True, schema=True):
    conn = FakeConnection()
    monkeypatch.setattr(setup_db.config, "load_dsn", lambda: "postgresql://x/y")
    monkeypatch.setattr(setup_db.config, "connect", lambda dsn=None, **kwargs: conn)
    monkeypatch.setattr(setup_db, "database_exists", lambda dsn: database)
    monkeypatch.setattr(setup_db, "schema_is_applied", lambda c: schema)
    monkeypatch.setattr(
        setup_db, "create_database", lambda dsn: conn.log.append("create_database")
    )
    monkeypatch.setattr(
        setup_db.sqlfiles, "run_sql_file", lambda c, name: c.log.append(f"file {name}")
    )
    return setup_db.run_setup(marts_only=True), conn


def test_marts_only_drops_and_recreates_the_marts_schema_and_nothing_else(monkeypatch):
    exit_code, conn = run_marts_only(monkeypatch)

    assert exit_code == 0
    assert conn.log == [
        "DROP SCHEMA IF EXISTS marts CASCADE",
        "CREATE SCHEMA marts",
        f"file {setup_db.sqlfiles.MARTS_FILE}",
        "commit",
    ]
    assert conn.closed


def test_marts_only_refuses_when_the_schema_is_not_applied(monkeypatch):
    exit_code, conn = run_marts_only(monkeypatch, schema=False)

    assert exit_code == 1
    assert conn.log == []


def test_marts_only_refuses_when_the_database_is_missing_and_does_not_create_it(monkeypatch):
    exit_code, conn = run_marts_only(monkeypatch, database=False)

    assert exit_code == 1
    assert conn.log == []
