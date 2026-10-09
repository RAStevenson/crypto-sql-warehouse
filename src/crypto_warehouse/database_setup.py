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

from . import database_connection, sql_file_runner
from .database_connection import OperatorError

logger = logging.getLogger(__name__)

WAREHOUSE_SCHEMAS = ("marts", "core", "raw")  # drop order: dependents first


def database_exists(database_url: str) -> bool:
    """Return True if a connection to database_url succeeds and False if it fails
    with an error whose text contains "does not exist".

    Any other connection error is re-raised.
    """
    try:
        connection = psycopg2.connect(database_url)
    except psycopg2.OperationalError as error:
        if "does not exist" in str(error):
            return False
        raise
    connection.close()
    return True


def create_database(database_url: str) -> bool:
    """Create database_url's database if it is missing and return True if one
    was created.

    Raises OperatorError when the database is missing and database_url is not a URL
    that database_connection.split_database_name can point at the maintenance database.
    """
    if database_exists(database_url):
        return False

    split_url = database_connection.split_database_name(database_url)
    if split_url is None:
        raise OperatorError(
            "the target database does not exist and DATABASE_URL is not a "
            "postgresql:// URL this can rewrite. Create the database by hand "
            "and re-run."
        )
    maintenance_url, database = split_url

    logger.info("database %r does not exist, creating it", database)
    # CREATE DATABASE cannot run inside a transaction block, hence autocommit.
    with (
        closing(database_connection.connect(maintenance_url, autocommit=True)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(database)))
    logger.info("created database %r", database)
    return True


def schema_is_applied(connection) -> bool:
    """Return True if core.assets exists, which is the test for an applied schema."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('core.assets') IS NOT NULL")
        return bool(cursor.fetchone()[0])


def needs_timeframes_migration(connection) -> bool:
    """Return True if the warehouse still has the 1-hour-only fact table
    core.ohlcv_hourly and not core.ohlcv, the shape migration 001 converts."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('core.ohlcv_hourly') IS NOT NULL "
            "AND to_regclass('core.ohlcv') IS NULL"
        )
        return bool(cursor.fetchone()[0])


def drop_schemas(connection) -> None:
    """Drop the marts, core, and raw schemas and everything in them.

    This does not commit, so the caller can commit the drop together with the
    rebuild and a DDL error leaves the old schemas in place.
    """
    logger.warning("dropping schemas %s and everything in them", ", ".join(WAREHOUSE_SCHEMAS))
    with connection.cursor() as cursor:
        for schema in WAREHOUSE_SCHEMAS:
            cursor.execute(
                pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pgsql.Identifier(schema))
            )


def rebuild_marts(connection) -> None:
    """Drop the marts schema and re-create it from sql/02_marts.sql, leaving
    raw and core untouched.

    This does not commit, so a DDL error leaves the old marts in place.
    """
    logger.warning(
        "dropping schema marts and rebuilding it from sql/%s", sql_file_runner.MARTS_FILE
    )
    with connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS marts CASCADE")
        cursor.execute("CREATE SCHEMA marts")
    sql_file_runner.run_sql_file(connection, sql_file_runner.MARTS_FILE)


def migrate_timeframes(database_url: str) -> int:
    """Convert a warehouse built before timeframes existed, keeping every
    stored candle and run record, and return a process exit code.

    Runs sql/migrations/001_add_timeframes.sql and then sql/02_marts.sql in
    one transaction, so a failure leaves the old warehouse as it was. A
    warehouse that is already converted, or has no schema, returns 1.
    """
    if not database_exists(database_url):
        logger.error("the database does not exist; run scripts/run_setup.py with no flag first")
        return 1
    with closing(database_connection.connect(database_url)) as connection:
        if not needs_timeframes_migration(connection):
            logger.error(
                "nothing to migrate: core.ohlcv_hourly is absent or core.ohlcv already "
                "exists, so the warehouse already has timeframes or has no schema"
            )
            return 1
        sql_file_runner.run_sql_file(connection, sql_file_runner.TIMEFRAMES_MIGRATION_FILE)
        sql_file_runner.run_sql_file(connection, sql_file_runner.MARTS_FILE)
        connection.commit()
    logger.info(
        "migrated: core.ohlcv_hourly is now core.ohlcv with every candle labeled 1h, "
        "core.timeframes is seeded with 1h active, and the marts are rebuilt"
    )
    return 0


def run_setup(*, reset: bool = False, marts_only: bool = False, migrate: bool = False) -> int:
    """Create the database if needed, apply the schema and mart DDL, and return
    a process exit code.

    A schema that is already applied returns 1 unless reset is set, in which
    case the schemas are dropped and rebuilt. With marts_only, only the marts
    schema is dropped and rebuilt, which is how a changed mart definition
    reaches a loaded warehouse. With migrate, a warehouse built before
    timeframes existed is converted in place.
    """
    database_url = database_connection.load_database_url()

    if migrate:
        return migrate_timeframes(database_url)

    if marts_only:
        if not database_exists(database_url):
            logger.error("the database does not exist; run scripts/run_setup.py with no flag first")
            return 1
        with closing(database_connection.connect(database_url)) as connection:
            if not schema_is_applied(connection):
                logger.error(
                    "core.assets does not exist, so there is nothing to rebuild the "
                    "marts from; run scripts/run_setup.py with no flag first"
                )
                return 1
            rebuild_marts(connection)
            connection.commit()
        logger.info("marts rebuilt; raw and core were not touched")
        return 0

    created = create_database(database_url)

    with closing(database_connection.connect(database_url)) as connection:
        if schema_is_applied(connection) and not created:
            if not reset:
                if needs_timeframes_migration(connection):
                    logger.error(
                        "this warehouse predates timeframes (it has core.ohlcv_hourly). "
                        "Re-run with --migrate to convert it in place and keep every "
                        "stored candle."
                    )
                    return 1
                logger.error(
                    "core.assets already exists; the schema is applied. "
                    "Re-run with --reset to drop schemas raw, core and marts "
                    "and rebuild them, which deletes every stored candle."
                )
                return 1
            drop_schemas(connection)

        # One transaction for the drop and both files: a database with the
        # fact tables but no matviews is a worse state to land in than no
        # database at all, and a failed rebuild must not have dropped the old
        # schemas.
        sql_file_runner.run_sql_file(connection, sql_file_runner.SCHEMA_FILE)
        sql_file_runner.run_sql_file(connection, sql_file_runner.MARTS_FILE)
        connection.commit()

    logger.info("setup complete; run scripts/run_ingest.py next")
    return 0
