"""Locating and running the numbered files in sql/.

They go through psycopg2 rather than a psql subprocess so nothing depends on
psql being installed or on PATH, which on Windows it usually is not. The cost
is that a file is one execute() call: it succeeds or fails as a unit, and
statements that refuse to run inside a transaction block (REFRESH ...
CONCURRENTLY) need a connection opened with autocommit=True.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import REPO_ROOT

log = logging.getLogger(__name__)

SQL_DIR = REPO_ROOT / "sql"

SCHEMA_FILE = "01_schema.sql"
MARTS_FILE = "02_marts.sql"
REFRESH_MARTS_FILE = "03_refresh_marts.sql"
QUERIES_FILE = "04_queries.sql"
VERIFY_FILE = "05_verify.sql"


class NoticeLogger(list):
    """Drop-in replacement for conn.notices that logs each server notice as it
    arrives.

    psycopg2 otherwise buffers notices in a plain list capped at 50 entries,
    which is exactly the wrong behavior for a verify script whose whole job
    is to show the RAISE NOTICE lines. Nothing is retained, so a long run
    cannot grow the list without bound.
    """

    def append(self, notice: str) -> None:
        log.info("postgres: %s", notice.strip())


def sql_path(name: str) -> Path:
    path = SQL_DIR / name
    if not path.is_file():
        available = sorted(p.name for p in SQL_DIR.glob("*.sql")) if SQL_DIR.is_dir() else []
        raise FileNotFoundError(
            f"{path} not found. sql/ currently holds: {available or 'nothing'}"
        )
    return path


def run_sql_file(conn, name: str) -> None:
    """Execute one file from sql/ on an open connection.

    Transaction handling is the caller's: this does not commit, so a caller on
    a normal connection controls the boundary and a caller that needs
    autocommit opens the connection that way.
    """
    path = sql_path(name)
    log.info("executing sql/%s", path.name)
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
