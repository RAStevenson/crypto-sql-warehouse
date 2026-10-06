"""Locating and running the numbered files in sql/.

They go through psycopg2 rather than a psql subprocess so nothing depends on
psql being installed or on PATH, which on Windows it usually is not. The cost
is that a file is one execute() call: it succeeds or fails as a unit, inside
whatever transaction the caller has open.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .database_connection import REPO_ROOT

log = logging.getLogger(__name__)

SQL_DIR = REPO_ROOT / "sql"

SCHEMA_FILE = "01_schema.sql"
MARTS_FILE = "02_marts.sql"
REFRESH_MARTS_FILE = "03_refresh_marts.sql"
QUERIES_FILE = "04_queries.sql"
VERIFY_FILE = "05_verify.sql"


# psycopg2 keeps notices in a plain list capped at 50 entries, and the verify
# script reports each passing check as a RAISE NOTICE line, so the list is
# replaced.
class NoticeLogger(list):
    """A replacement for conn.notices that logs each server notice as it arrives.

    Nothing is stored, so the list stays empty however long the run is.
    """

    def append(self, notice: str) -> None:
        """Log one server notice instead of storing it."""
        log.info("postgres: %s", notice.strip())


def sql_path(name: str) -> Path:
    """Return the path of a named file in sql/, raising FileNotFoundError that
    lists the files present when it is missing."""
    path = SQL_DIR / name
    if not path.is_file():
        available = (
            sorted(sql_file.name for sql_file in SQL_DIR.glob("*.sql"))
            if SQL_DIR.is_dir()
            else []
        )
        raise FileNotFoundError(
            f"{path} not found. sql/ currently holds: {available or 'nothing'}"
        )
    return path


def run_sql_file(conn, name: str) -> None:
    """Execute one file from sql/ on an open connection in a single execute() call.

    This does not commit, so the caller controls the transaction.
    """
    path = sql_path(name)
    log.info("executing sql/%s", path.name)
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
