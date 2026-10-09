"""Running the dbt project in dbt/ from the warehouse's own scripts.

dbt owns the marts layer. Each mart is a model under dbt/models/marts/, and
the checks on the sources and the marts are dbt tests. dbt reads its
connection from dbt/profiles.yml, which takes host, port, user, password,
and database from five environment variables. This module fills those from
DATABASE_URL, so the one connection string in .env serves both psycopg2 and
dbt, and then runs dbt in this process through its programmatic runner.
"""

from __future__ import annotations

import getpass
import logging
import os
from urllib.parse import unquote, urlsplit

from .database_connection import REPO_ROOT, OperatorError, load_database_url

logger = logging.getLogger(__name__)

DBT_DIR = REPO_ROOT / "dbt"
DBT_LOG_DIR = REPO_ROOT / "logs" / "dbt"

# The variables dbt/profiles.yml reads, each with this prefix: HOST, PORT,
# USER, PASSWORD, DATABASE.
ENVIRONMENT_PREFIX = "CRYPTO_WAREHOUSE_PG"


def connection_environment(database_url: str) -> dict[str, str]:
    """Return the five environment variables dbt/profiles.yml reads, taken
    from a postgresql:// URL.

    Raises OperatorError for the libpq keyword form or for a URL that names
    no host or no database, because dbt takes the parts and not the whole.
    A URL with no user falls back to the operating-system user, as psycopg2
    does.
    """
    parts = urlsplit(database_url)
    if parts.scheme not in ("postgresql", "postgres"):
        raise OperatorError(
            "dbt needs DATABASE_URL in the form "
            "postgresql://user:password@host:port/database; the keyword form "
            "(host=... dbname=...) cannot be split into the parts dbt takes"
        )
    database = parts.path.lstrip("/")
    if not parts.hostname or not database:
        raise OperatorError("DATABASE_URL names no host or no database, and dbt needs both")
    return {
        f"{ENVIRONMENT_PREFIX}HOST": parts.hostname,
        f"{ENVIRONMENT_PREFIX}PORT": str(parts.port or 5432),
        f"{ENVIRONMENT_PREFIX}USER": (
            unquote(parts.username) if parts.username else getpass.getuser()
        ),
        f"{ENVIRONMENT_PREFIX}PASSWORD": unquote(parts.password or ""),
        f"{ENVIRONMENT_PREFIX}DATABASE": database,
    }


def run_dbt(arguments: list[str], database_url: str | None = None) -> int:
    """Run one dbt command against the project in dbt/ and return a process
    exit code, 0 when dbt succeeded and 1 when a model or test failed or dbt
    could not run at all.

    The connection comes from database_url, or from DATABASE_URL when none is
    given. dbt is imported inside the function because importing it takes a
    couple of seconds and nothing else in the package needs it.
    """
    os.environ.update(connection_environment(database_url or load_database_url()))
    from dbt.cli.main import dbtRunner

    command = [
        *arguments,
        "--project-dir",
        str(DBT_DIR),
        "--profiles-dir",
        str(DBT_DIR),
        "--log-path",
        str(DBT_LOG_DIR),
    ]
    logger.info("dbt %s", " ".join(arguments))
    result = dbtRunner().invoke(command)
    if result.exception is not None:
        # A failure before the run itself: a bad profile, a parse error, a
        # connection refused. dbt's own result would otherwise say nothing.
        logger.error("dbt %s did not run: %s", arguments[0] if arguments else "", result.exception)
        return 1
    return 0 if result.success else 1


def build_marts(database_url: str | None = None) -> int:
    """Build the marts that are missing and refresh the ones that exist, in
    dependency order (`dbt run`), and return dbt's exit code."""
    return run_dbt(["run"], database_url)


def test_warehouse(database_url: str | None = None) -> int:
    """Run every test in the dbt project against the loaded warehouse
    (`dbt test`) and return dbt's exit code."""
    return run_dbt(["test"], database_url)
