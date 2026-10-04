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
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and edit it, "
            "or export DATABASE_URL directly."
        )
    return dsn


def connect(dsn: str | None = None, *, autocommit: bool = False):
    """Open a psycopg2 connection to the DSN, or to DATABASE_URL when none is
    given, and return it.

    Pass autocommit=True for statements that cannot run inside a transaction
    block, such as CREATE DATABASE.
    """
    conn = psycopg2.connect(dsn or load_dsn())
    conn.autocommit = autocommit
    return conn


def split_database_name(dsn: str) -> tuple[str, str] | None:
    """Return (maintenance_dsn, database_name) for a URL-style DSN.

    Returns None for the libpq keyword form ("host=... dbname=...") and for a
    URL that names no database.
    """
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgresql", "postgres"):
        return None
    database = parts.path.lstrip("/")
    if not database:
        return None
    maintenance = urlunsplit(parts._replace(path=f"/{MAINTENANCE_DATABASE}"))
    return maintenance, database


class _UtcFormatter(logging.Formatter):
    """Formats log timestamps in UTC, so a log line can be matched to the
    warehouse's UTC bucket boundaries."""

    converter = time.gmtime


def configure_logging(verbose: bool = False) -> None:
    """Send timestamped log lines to stderr at INFO level, or DEBUG when verbose.

    A second call adds no second handler, so calling it twice does not double
    every line.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            _UtcFormatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
