"""What each error handler catches, what it lets through, and what it prints.

The rule these hold: a handler catches only the failures it expects, prints
the full error first, and ends on one plain line saying what was written or
skipped and what to rerun. Anything it does not expect reaches the caller
with its traceback.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest

from crypto_warehouse import warehouse_verification
from crypto_warehouse.database_connection import OperatorError

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    """Import a script from scripts/ by path, as a module of its own."""
    spec = importlib.util.spec_from_file_location(f"script_{name}", SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def error_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


# --------------------------------------------------------------------------
# scripts/run_add_asset.py
# --------------------------------------------------------------------------
@pytest.fixture()
def add_asset_script(monkeypatch):
    module = load_script("run_add_asset")
    monkeypatch.setattr(sys, "argv", ["run_add_asset.py", "XRP-USD", "--no-backfill"])
    return module


def test_a_failed_probe_prints_the_traceback_then_says_nothing_was_written(
    add_asset_script, monkeypatch, caplog
):
    def gave_up(session, product_code):
        raise RuntimeError("XRP-USD: gave up fetching after 5 attempts")

    def must_not_connect():
        raise AssertionError("the database must not be reached after a failed probe")

    monkeypatch.setattr(add_asset_script, "probe_product", gave_up)
    monkeypatch.setattr(add_asset_script, "connect", must_not_connect)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as stopped:
            add_asset_script.main()
        assert stopped.value.code == 2

    errors = error_records(caplog)
    assert errors[0].exc_info is not None
    assert "gave up fetching" in caplog.text
    assert "XRP-USD was not registered and nothing was written" in errors[-1].getMessage()


def test_a_probe_that_finds_no_candles_prints_its_own_message(
    add_asset_script, monkeypatch, caplog
):
    def empty(session, product_code):
        raise OperatorError("XRP-USD: Coinbase answered but returned no candles")

    monkeypatch.setattr(add_asset_script, "probe_product", empty)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as stopped:
            add_asset_script.main()
        assert stopped.value.code == 2

    messages = [record.getMessage() for record in error_records(caplog)]
    assert messages == ["XRP-USD: Coinbase answered but returned no candles"]


def test_a_database_error_after_the_probe_is_not_reported_as_a_probe_failure(
    add_asset_script, monkeypatch
):
    def refuse():
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(add_asset_script, "probe_product", lambda session, product_code: 3)
    monkeypatch.setattr(add_asset_script, "connect", refuse)

    with pytest.raises(psycopg2.OperationalError):
        add_asset_script.main()


# --------------------------------------------------------------------------
# crypto_warehouse.warehouse_verification
# --------------------------------------------------------------------------
class FakeConnection:
    """Stands in for a psycopg2 connection; run_sql_file is replaced, so only
    close and the notices attribute are used."""

    notices: object = None

    def close(self):
        pass


def run_verify_raising(monkeypatch, error: Exception | None, dbt_exit_code: int = 0) -> int:
    def run_sql_file(connection, name):
        if error is not None:
            raise error

    monkeypatch.setattr(
        warehouse_verification.database_connection, "connect", lambda **kwargs: FakeConnection()
    )
    monkeypatch.setattr(warehouse_verification.sql_file_runner, "run_sql_file", run_sql_file)
    monkeypatch.setattr(warehouse_verification.dbt_project, "test_warehouse", lambda: dbt_exit_code)
    return warehouse_verification.run_verify()


def test_a_failed_check_prints_the_server_report_then_the_summary(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        exit_code = run_verify_raising(
            monkeypatch, psycopg2.errors.RaiseException("timezone check failed: 3 row(s)")
        )

    assert exit_code == 1
    messages = [record.getMessage() for record in error_records(caplog)]
    assert messages[0].startswith("full server report:")
    assert messages[1].startswith("check FAILED: timezone check failed")
    assert messages[-1] == "verification FAILED"


def test_failed_dbt_tests_fail_verify_and_the_sql_check_still_runs(monkeypatch, caplog):
    ran: list[str] = []

    def run_sql_file(connection, name):
        ran.append(name)

    monkeypatch.setattr(
        warehouse_verification.database_connection, "connect", lambda **kwargs: FakeConnection()
    )
    monkeypatch.setattr(warehouse_verification.sql_file_runner, "run_sql_file", run_sql_file)
    monkeypatch.setattr(warehouse_verification.dbt_project, "test_warehouse", lambda: 1)

    with caplog.at_level(logging.ERROR):
        exit_code = warehouse_verification.run_verify()

    assert exit_code == 1
    assert ran == [warehouse_verification.sql_file_runner.VERIFY_FILE]
    messages = [record.getMessage() for record in error_records(caplog)]
    assert messages[0].startswith("dbt test reported failures")
    assert messages[-1] == "verification FAILED"


def test_a_database_error_that_is_not_a_check_is_let_through(monkeypatch):
    with pytest.raises(psycopg2.OperationalError):
        run_verify_raising(monkeypatch, psycopg2.OperationalError("server closed the connection"))
