"""Connection settings and logging setup.

This module is the one place that turns DATABASE_URL into a psycopg2
connection, so every entry point in scripts/ gets the same behavior and the
same error message when the variable is missing.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

# The database psycopg2 connects to when it needs a session that is not inside
# the target database, which is the case when the target does not exist yet.
MAINTENANCE_DATABASE = "postgres"


class ConfigError(RuntimeError):
    """Raised for a problem the operator has to fix before the pipeline can run."""


def load_dsn() -> str:
    """Return DATABASE_URL from the environment, falling back to the repo's .env file.

    A variable already exported takes precedence over the .env value, and
    ConfigError is raised when neither source sets it.
    """
    # Read by absolute path, not by searching upward from the working
    # directory, so the scripts behave the same wherever they are launched.
    load_dotenv(ENV_FILE)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and edit it, "
            "or export DATABASE_URL directly."
        )
    return database_url


def connect(database_url: str | None = None, *, autocommit: bool = False):
    """Open a psycopg2 connection to database_url, or to DATABASE_URL when none
    is given, and return it.

    Pass autocommit=True for statements that cannot run inside a transaction
    block, such as CREATE DATABASE.
    """
    connection = psycopg2.connect(database_url or load_dsn())
    connection.autocommit = autocommit
    return connection


def split_database_name(database_url: str) -> tuple[str, str] | None:
    """Return (maintenance_url, database_name) for a URL-style DSN.

    Returns None for the libpq keyword form ("host=... dbname=...") and for a
    URL that names no database.
    """
    url_parts = urlsplit(database_url)
    if url_parts.scheme not in ("postgresql", "postgres"):
        return None
    database = url_parts.path.lstrip("/")
    if not database:
        return None
    maintenance_url = urlunsplit(url_parts._replace(path=f"/{MAINTENANCE_DATABASE}"))
    return maintenance_url, database


class _UtcFormatter(logging.Formatter):
    """Formats log timestamps in UTC, so a log line can be matched to the
    warehouse's UTC bucket boundaries."""

    converter = time.gmtime


def configure_logging(verbose: bool = False) -> None:
    """Send timestamped log lines to stderr at INFO level, or DEBUG when verbose.

    A second call adds no second handler, so calling it twice does not double
    every line.
    """
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            _UtcFormatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
