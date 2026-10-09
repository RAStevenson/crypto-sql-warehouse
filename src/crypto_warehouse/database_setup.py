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

from . import database_connection, dbt_project, sql_file_runner
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


def needs_fetch_windows_migration(connection) -> bool:
    """Return True if the schema is applied but raw.fetch_windows, which
    migration 002 adds, does not exist yet."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('core.assets') IS NOT NULL "
            "AND to_regclass('raw.fetch_windows') IS NULL"
        )
        return bool(cursor.fetchone()[0])


def pending_migrations(connection) -> list[str]:
    """Return the migration files in sql/migrations/ this warehouse still
    needs, in the order they must run."""
    pending = []
    if needs_timeframes_migration(connection):
        pending.append(sql_file_runner.TIMEFRAMES_MIGRATION_FILE)
    if needs_fetch_windows_migration(connection):
        pending.append(sql_file_runner.FETCH_WINDOWS_MIGRATION_FILE)
    return pending


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


def drop_marts(connection) -> None:
    """Drop the marts schema and re-create it empty, leaving raw and core
    untouched, and commit, so that dbt can build the marts afresh."""
    logger.warning("dropping schema marts; dbt rebuilds it from dbt/models/marts/")
    with connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS marts CASCADE")
        cursor.execute("CREATE SCHEMA marts")
    connection.commit()


def run_migrations(database_url: str) -> int:
    """Apply every migration this warehouse still needs, keeping every stored
    candle and run record, and return a process exit code.

    The pending files run in order in one transaction, so a failure leaves
    the old warehouse as it was. Migration 001 drops the marts, so when it
    runs, dbt builds them again once the transaction has committed. A
    warehouse with nothing pending, or with no schema, returns 1.
    """
    if not database_exists(database_url):
        logger.error("the database does not exist; run scripts/run_setup.py with no flag first")
        return 1
    with closing(database_connection.connect(database_url)) as connection:
        if not schema_is_applied(connection):
            logger.error("core.assets does not exist; run scripts/run_setup.py with no flag first")
            return 1
        pending = pending_migrations(connection)
        if not pending:
            logger.error("nothing to migrate: this warehouse already has every migration")
            return 1
        for name in pending:
            sql_file_runner.run_sql_file(connection, name)
        connection.commit()
    logger.info("migrated: applied %s", ", ".join(pending))
    if (
        sql_file_runner.TIMEFRAMES_MIGRATION_FILE in pending
        and dbt_project.build_marts(database_url) != 0
    ):
        logger.error(
            "the migration is applied but dbt could not build the marts; fix the "
            "cause and run scripts/run_setup.py --rebuild-marts"
        )
        return 1
    if sql_file_runner.FETCH_WINDOWS_MIGRATION_FILE in pending:
        logger.info(
            "runs loaded before raw.fetch_windows existed have no windows on record, so "
            "the dbt test gaps_without_a_recorded_request fails on their gaps until "
            "scripts/run_ingest.py --recheck-gaps runs"
        )
    return 0


def run_setup(*, reset: bool = False, marts_only: bool = False, migrate: bool = False) -> int:
    """Create the database if needed, apply the schema DDL, have dbt build the
    marts, and return a process exit code.

    A schema that is already applied returns 1 unless reset is set, in which
    case the schemas are dropped and rebuilt. With marts_only, only the marts
    schema is dropped and rebuilt, which is how a changed mart definition
    reaches a loaded warehouse. With migrate, a warehouse built before the
    current schema is brought up to date in place.
    """
    database_url = database_connection.load_database_url()

    if migrate:
        return run_migrations(database_url)

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
            drop_marts(connection)
        if dbt_project.build_marts(database_url) != 0:
            logger.error("dbt could not build the marts; the schema marts is empty until it can")
            return 1
        logger.info("marts rebuilt; raw and core were not touched")
        return 0

    created = create_database(database_url)

    with closing(database_connection.connect(database_url)) as connection:
        if schema_is_applied(connection) and not created:
            if not reset:
                if pending_migrations(connection):
                    logger.error(
                        "this warehouse predates the current schema. Re-run with "
                        "--migrate to bring it up to date in place and keep every "
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

        # One transaction for the drop and the schema file, so a failed
        # rebuild cannot have dropped the old schemas. dbt builds the marts
        # after the commit, in its own connection.
        sql_file_runner.run_sql_file(connection, sql_file_runner.SCHEMA_FILE)
        connection.commit()

    if dbt_project.build_marts(database_url) != 0:
        logger.error(
            "the schema is applied but dbt could not build the marts; fix the cause and "
            "run scripts/run_setup.py --rebuild-marts"
        )
        return 1
    logger.info("setup complete; run scripts/run_ingest.py next")
    return 0
