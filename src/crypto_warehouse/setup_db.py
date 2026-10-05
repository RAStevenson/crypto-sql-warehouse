"""Creating the database and applying the DDL without needing psql.

psql is the usual tool for this, but on Windows it ships inside the PostgreSQL
install directory and is almost never on PATH, so a README step that runs
psql -f sql/01_schema.sql would fail on a fresh machine. psycopg2 is already a
dependency and can execute the same files.
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
    """Return True if a connection to the DSN succeeds and False if it fails
    with an error whose text contains "does not exist".

    Any other connection error is re-raised.
    """
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as exc:
        if "does not exist" in str(exc):
            return False
        raise
    conn.close()
    return True


def create_database(dsn: str) -> bool:
    """Create the DSN's database if it is missing and return True if one was
    created.

    Raises ConfigError when the database is missing and the DSN is not a URL
    that config.split_database_name can point at the maintenance database.
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
    """Return True if core.assets exists, which is the test for an applied schema."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('core.assets') IS NOT NULL")
        return bool(cur.fetchone()[0])


def drop_schemas(conn) -> None:
    """Drop the marts, core, and raw schemas and everything in them.

    This does not commit, so the caller can commit the drop together with the
    rebuild and a DDL error leaves the old schemas in place.
    """
    log.warning("dropping schemas %s and everything in them", ", ".join(SCHEMAS))
    with conn.cursor() as cur:
        for schema in SCHEMAS:
            cur.execute(
                pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pgsql.Identifier(schema))
            )


def rebuild_marts(conn) -> None:
    """Drop the marts schema and re-create it from sql/02_marts.sql, leaving
    raw and core untouched.

    This does not commit, so a DDL error leaves the old marts in place.
    """
    log.warning("dropping schema marts and rebuilding it from sql/%s", sqlfiles.MARTS_FILE)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS marts CASCADE")
        cur.execute("CREATE SCHEMA marts")
    sqlfiles.run_sql_file(conn, sqlfiles.MARTS_FILE)


def run_setup(*, reset: bool = False, marts_only: bool = False) -> int:
    """Create the database if needed, apply the schema and mart DDL, and return
    a process exit code.

    A schema that is already applied returns 1 unless reset is set, in which
    case the schemas are dropped and rebuilt. With marts_only, only the marts
    schema is dropped and rebuilt, which is how a changed mart definition
    reaches a loaded warehouse.
    """
    dsn = config.load_dsn()

    if marts_only:
        if not database_exists(dsn):
            log.error("the database does not exist; run scripts/run_setup.py with no flag first")
            return 1
        with closing(config.connect(dsn)) as conn:
            if not schema_is_applied(conn):
                log.error(
                    "core.assets does not exist, so there is nothing to rebuild the "
                    "marts from; run scripts/run_setup.py with no flag first"
                )
                return 1
            rebuild_marts(conn)
            conn.commit()
        log.info("marts rebuilt; raw and core were not touched")
        return 0

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

        # One transaction for the drop and both files: a database with the
        # fact tables but no matviews is a worse state to land in than no
        # database at all, and a failed rebuild must not have dropped the old
        # schemas.
        sqlfiles.run_sql_file(conn, sqlfiles.SCHEMA_FILE)
        sqlfiles.run_sql_file(conn, sqlfiles.MARTS_FILE)
        conn.commit()

    log.info("setup complete; run scripts/run_ingest.py next")
    return 0
