"""Creating the database and applying the DDL without needing psql.

psql is the obvious tool for this and it is not the one available: on Windows
it ships inside the PostgreSQL install directory and is almost never on PATH,
so "just run psql -f sql/01_schema.sql" is a step that fails for the person
following the README on a fresh machine. psycopg2 is already a dependency and
can execute the same files.
"""

from __future__ import annotations

import logging
from contextlib import closing

import psycopg2
from psycopg2 import sql as pgsql

from . import config, sqlfiles
from .config import ConfigError

log = logging.getLogger(__name__)

SCHEMAS = ("marts", "core", "raw")  # drop order: dependents first


def database_exists(dsn: str) -> bool:
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as exc:
        if "does not exist" in str(exc):
            return False
        raise
    conn.close()
    return True


def create_database(dsn: str) -> bool:
    """Create the DSN's database if it is missing. Returns True if it created
    one.

    Raises ConfigError rather than guessing when the DSN is not a URL this can
    rewrite, so the operator gets a clear instruction instead of a confusing
    connection error.
    """
    if database_exists(dsn):
        return False

    parts = config.split_database_name(dsn)
    if parts is None:
        raise ConfigError(
            "the target database does not exist and DATABASE_URL is not a "
            "postgresql:// URL this can rewrite. Create the database by hand "
            "and re-run."
        )
    maintenance_dsn, database = parts

    log.info("database %r does not exist, creating it", database)
    # CREATE DATABASE cannot run inside a transaction block, hence autocommit.
    with closing(config.connect(maintenance_dsn, autocommit=True)) as conn, conn.cursor() as cur:
        cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(database)))
    log.info("created database %r", database)
    return True


def schema_is_applied(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('core.assets') IS NOT NULL")
        return bool(cur.fetchone()[0])


def drop_schemas(conn) -> None:
    """Tear down the three schemas so the DDL can be applied from scratch.

    Destructive and only reachable behind an explicit flag. The DDL files use
    plain CREATE rather than CREATE IF NOT EXISTS, which is the right choice
    for a file that is meant to describe one exact schema, and it means
    re-applying them needs the old objects gone first.
    """
    log.warning("dropping schemas %s and everything in them", ", ".join(SCHEMAS))
    with conn.cursor() as cur:
        for schema in SCHEMAS:
            cur.execute(
                pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pgsql.Identifier(schema))
            )
    conn.commit()


def run_setup(*, reset: bool = False) -> int:
    """Create the database if needed, then apply the schema and mart DDL.

    Returns a process exit code.
    """
    dsn = config.load_dsn()
    created = create_database(dsn)

    with closing(config.connect(dsn)) as conn:
        if schema_is_applied(conn) and not created:
            if not reset:
                log.error(
                    "core.assets already exists; the schema is applied. "
                    "Re-run with --reset to drop schemas raw, core and marts "
                    "and rebuild them, which deletes every stored candle."
                )
                return 1
            drop_schemas(conn)

        # One transaction for both files: a database with the fact tables but
        # no matviews is a worse state to land in than no database at all.
        sqlfiles.run_sql_file(conn, sqlfiles.SCHEMA_FILE)
        sqlfiles.run_sql_file(conn, sqlfiles.MARTS_FILE)
        conn.commit()

    log.info("setup complete; run scripts/run_ingest.py next")
    return 0
