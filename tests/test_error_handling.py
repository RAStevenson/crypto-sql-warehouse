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

from crypto_warehouse import diversification_metrics, warehouse_verification
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
# scripts/run_diversification_analysis.py
# --------------------------------------------------------------------------
@pytest.fixture()
def analysis_script(monkeypatch):
    module = load_script("run_diversification_analysis")
    monkeypatch.setattr(sys, "argv", ["run_diversification_analysis.py"])
    return module


def fake_metrics() -> dict:
    return {
        "diversification": {
            "equal_weight_portfolio_vol": 0.5,
            "average_single_asset_vol": 0.55,
            "risk_removed_actual": 0.1,
            "risk_removed_if_uncorrelated": 0.58,
            "diversification_captured": 0.17,
        }
    }


def test_analysis_script_reports_an_unreachable_database_with_its_traceback(
    analysis_script, monkeypatch, caplog
):
    def refuse(*args, **kwargs):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(analysis_script, "run_diversification_analysis", refuse)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as stopped:
            analysis_script.main()
        assert stopped.value.code == 1

    errors = error_records(caplog)
    assert errors[0].exc_info is not None
    assert "connection refused" in caplog.text
    assert errors[-1].getMessage().startswith("nothing was written")
    assert "DATABASE_URL" in errors[-1].getMessage()


def test_analysis_script_reports_missing_tables_and_says_to_run_setup(
    analysis_script, monkeypatch, caplog
):
    def missing(*args, **kwargs):
        raise psycopg2.errors.UndefinedTable('relation "marts.daily_metrics" does not exist')

    monkeypatch.setattr(analysis_script, "run_diversification_analysis", missing)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as stopped:
            analysis_script.main()
        assert stopped.value.code == 1

    errors = error_records(caplog)
    assert errors[0].exc_info is not None
    assert "run_setup.py" in errors[-1].getMessage()


def test_analysis_script_reports_thin_data_and_says_nothing_was_written(
    analysis_script, monkeypatch, caplog
):
    def thin(*args, **kwargs):
        raise diversification_metrics.PanelDataError("no non-null log returns found")

    monkeypatch.setattr(analysis_script, "run_diversification_analysis", thin)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as stopped:
            analysis_script.main()
        assert stopped.value.code == 1

    messages = [record.getMessage() for record in error_records(caplog)]
    assert messages == ["no non-null log returns found", "nothing was written"]


@pytest.mark.parametrize(
    "unexpected",
    [
        KeyError("diversification"),
        ValueError("a ValueError from somewhere else"),
        psycopg2.errors.SyntaxError("syntax error at or near SELEKT"),
    ],
    ids=["KeyError", "ValueError", "other database error"],
)
def test_analysis_script_lets_an_unexpected_error_through(analysis_script, monkeypatch, unexpected):
    def fail(*args, **kwargs):
        raise unexpected

    monkeypatch.setattr(analysis_script, "run_diversification_analysis", fail)

    with pytest.raises(type(unexpected)):
        analysis_script.main()


def test_a_failed_report_build_prints_the_traceback_then_what_was_written(
    analysis_script, monkeypatch, caplog
):
    def broken_build():
        raise KeyError("provenance")

    monkeypatch.setattr(
        analysis_script, "run_diversification_analysis", lambda *args: fake_metrics()
    )
    monkeypatch.setattr(analysis_script, "build_report", broken_build)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as stopped:
            analysis_script.main()
        assert stopped.value.code == 1

    errors = error_records(caplog)
    assert errors[0].exc_info is not None
    assert "KeyError" in caplog.text
    last_message = errors[-1].getMessage()
    assert "the metrics and figures were written" in last_message
    assert "run_build_report.py" in last_message


def test_a_report_build_that_exits_with_a_message_is_reported_the_same_way(
    analysis_script, monkeypatch, caplog
):
    def uncited():
        raise SystemExit("not cited from body prose: Table 3")

    monkeypatch.setattr(
        analysis_script, "run_diversification_analysis", lambda *args: fake_metrics()
    )
    monkeypatch.setattr(analysis_script, "build_report", uncited)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as stopped:
            analysis_script.main()
        assert stopped.value.code == 1

    assert "not cited from body prose: Table 3" in caplog.text
    assert "the metrics and figures were written" in error_records(caplog)[-1].getMessage()


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


def run_verify_raising(monkeypatch, error: Exception) -> int:
    def run_sql_file(connection, name):
        raise error

    monkeypatch.setattr(
        warehouse_verification.database_connection, "connect", lambda **kwargs: FakeConnection()
    )
    monkeypatch.setattr(warehouse_verification.sql_file_runner, "run_sql_file", run_sql_file)
    return warehouse_verification.run_verify()


def test_a_failed_check_prints_the_server_report_then_the_summary(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        exit_code = run_verify_raising(
            monkeypatch, psycopg2.errors.RaiseException("gap check failed: 1 hour(s) missing")
        )

    assert exit_code == 1
    messages = [record.getMessage() for record in error_records(caplog)]
    assert messages[0].startswith("full server report:")
    assert messages[-1].startswith("verification FAILED: gap check failed")
    assert "did not run" in messages[-1]


def test_a_database_error_that_is_not_a_check_is_let_through(monkeypatch):
    with pytest.raises(psycopg2.OperationalError):
        run_verify_raising(monkeypatch, psycopg2.OperationalError("server closed the connection"))
