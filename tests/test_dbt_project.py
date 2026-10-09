"""The dbt project's shape, held against the rest of the repo, and the
connection settings the scripts hand it. No database and no network; the
one test that runs dbt only parses the project."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from crypto_warehouse import dbt_project, report_generator
from crypto_warehouse.database_connection import OperatorError
from crypto_warehouse.dbt_project import ENVIRONMENT_PREFIX, connection_environment

REPO_ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = REPO_ROOT / "dbt"
MODELS_DIR = DBT_DIR / "models" / "marts"
SINGULAR_TESTS_DIR = DBT_DIR / "tests"

MATERIALIZED_VIEWS = {
    "daily_ohlcv": ["asset_id", "trade_date"],
    "daily_metrics": ["asset_id", "trade_date"],
    "pairwise_correlations_90d": ["asset_id_a", "asset_id_b"],
}


# --------------------------------------------------------------------------
# The connection dbt is handed
# --------------------------------------------------------------------------
def test_connection_environment_splits_a_url_into_the_five_variables():
    environment = connection_environment("postgresql://alice:s3cret@db.example:5433/warehouse")

    assert environment == {
        f"{ENVIRONMENT_PREFIX}HOST": "db.example",
        f"{ENVIRONMENT_PREFIX}PORT": "5433",
        f"{ENVIRONMENT_PREFIX}USER": "alice",
        f"{ENVIRONMENT_PREFIX}PASSWORD": "s3cret",
        f"{ENVIRONMENT_PREFIX}DATABASE": "warehouse",
    }


def test_connection_environment_defaults_the_port_and_decodes_the_password():
    environment = connection_environment("postgres://bob:p%40ss%2Fword@localhost/crypto")

    assert environment[f"{ENVIRONMENT_PREFIX}PORT"] == "5432"
    assert environment[f"{ENVIRONMENT_PREFIX}PASSWORD"] == "p@ss/word"
    assert environment[f"{ENVIRONMENT_PREFIX}DATABASE"] == "crypto"


def test_connection_environment_falls_back_to_the_os_user_like_psycopg2(monkeypatch):
    monkeypatch.setattr(dbt_project.getpass, "getuser", lambda: "robert")

    environment = connection_environment("postgresql://localhost/crypto")

    assert environment[f"{ENVIRONMENT_PREFIX}USER"] == "robert"
    assert environment[f"{ENVIRONMENT_PREFIX}PASSWORD"] == ""


@pytest.mark.parametrize(
    "database_url",
    [
        "host=localhost dbname=crypto user=postgres",  # libpq keyword form
        "postgresql:///crypto",  # no host
        "postgresql://localhost",  # no database
    ],
)
def test_connection_environment_rejects_what_dbt_cannot_take(database_url):
    with pytest.raises(OperatorError):
        connection_environment(database_url)


def test_the_profile_reads_every_variable_from_the_environment_and_holds_no_secret():
    profile = (DBT_DIR / "profiles.yml").read_text(encoding="utf-8")

    for suffix in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
        assert f"env_var('{ENVIRONMENT_PREFIX}{suffix}'" in profile
    # Nothing literal that would tie the project to one machine.
    assert "localhost" not in profile
    assert re.search(r"password:\s*\"\{\{", profile)


# --------------------------------------------------------------------------
# The models
# --------------------------------------------------------------------------
@pytest.mark.parametrize(("model", "index_columns"), sorted(MATERIALIZED_VIEWS.items()))
def test_each_materialized_view_declares_the_unique_index_a_concurrent_refresh_needs(
    model, index_columns
):
    source = (MODELS_DIR / f"{model}.sql").read_text(encoding="utf-8")
    config = source[: source.index("}}")]

    assert "'unique': True" in config
    for column in index_columns:
        assert f"'{column}'" in config


def test_the_project_makes_the_marts_materialized_views_refreshed_concurrently():
    project = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text(encoding="utf-8"))
    macro = (DBT_DIR / "macros" / "refresh_concurrently.sql").read_text(encoding="utf-8")

    assert project["models"]["crypto_warehouse"]["marts"]["+materialized"] == "materialized_view"
    assert "{% macro postgres__refresh_materialized_view(relation) %}" in macro
    assert "refresh materialized view concurrently" in macro


def test_the_tableau_extract_is_a_plain_view():
    source = (MODELS_DIR / "tableau_daily_extract.sql").read_text(encoding="utf-8")

    assert "materialized='view'" in source


def test_the_models_read_sources_and_refs_never_bare_table_names():
    for path in sorted(MODELS_DIR.glob("*.sql")):
        body = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--")
        )
        assert not re.search(r"\b(core|raw|marts)\.\w+", body), path.name


def test_every_table_the_schema_creates_is_a_declared_source():
    schema_sql = (REPO_ROOT / "sql" / "01_schema.sql").read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE TABLE (\w+)\.(\w+)", schema_sql))
    sources = yaml.safe_load((DBT_DIR / "models" / "sources.yml").read_text(encoding="utf-8"))
    declared = {
        (source["name"], table["name"])
        for source in sources["sources"]
        for table in source["tables"]
    }

    assert created == declared


# --------------------------------------------------------------------------
# The tests
# --------------------------------------------------------------------------
def test_every_singular_test_explains_itself_and_names_a_reason_column():
    paths = sorted(SINGULAR_TESTS_DIR.glob("*.sql"))
    assert len(paths) >= 10
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert source.startswith("-- "), path.name
        assert "AS reason" in source, path.name
        assert not source.rstrip().endswith(";"), path.name


def test_count_dbt_tests_counts_the_files_and_the_declared_tests():
    singular = len(list(SINGULAR_TESTS_DIR.glob("*.sql")))
    declared = 0
    for yml_path in DBT_DIR.glob("models/**/*.yml"):
        declared += yml_path.read_text(encoding="utf-8").count("data_tests:")
    # Every data_tests list in the project holds at least one item, and the
    # lists are small enough to add by hand: this recount is independent of
    # the generator's walk of the parsed yml.
    expected = singular + sum(
        len(value)
        for yml_path in DBT_DIR.glob("models/**/*.yml")
        for value in _data_test_lists(yaml.safe_load(yml_path.read_text(encoding="utf-8")))
    )
    assert declared > 0
    assert report_generator.count_dbt_tests() == expected


def _data_test_lists(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "data_tests":
                yield value
            else:
                yield from _data_test_lists(value)
    elif isinstance(node, list):
        for item in node:
            yield from _data_test_lists(item)


# --------------------------------------------------------------------------
# dbt itself can read the project
# --------------------------------------------------------------------------
def test_dbt_parses_the_project_with_no_database(monkeypatch, tmp_path):
    """`dbt parse` compiles every model and test and builds the lineage graph
    without connecting, so a broken ref(), source(), or yml fails here."""
    for suffix, value in (
        ("HOST", "nowhere.invalid"),
        ("PORT", "5432"),
        ("USER", "nobody"),
        ("PASSWORD", ""),
        ("DATABASE", "none"),
    ):
        monkeypatch.setenv(f"{ENVIRONMENT_PREFIX}{suffix}", value)
    from dbt.cli.main import dbtRunner

    result = dbtRunner().invoke(
        [
            "parse",
            "--project-dir",
            str(DBT_DIR),
            "--profiles-dir",
            str(DBT_DIR),
            "--target-path",
            str(tmp_path / "target"),
            "--log-path",
            str(tmp_path / "logs"),
            "--quiet",
        ]
    )

    assert result.exception is None, result.exception
    assert result.success
    assert os.path.isfile(tmp_path / "target" / "manifest.json")
