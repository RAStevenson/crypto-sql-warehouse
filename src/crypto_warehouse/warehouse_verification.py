"""Running the warehouse checks and turning their result into an exit code.

The checks come in two kinds. The dbt tests in dbt/tests/ and the dbt yml
files are each one SELECT that returns the rows that break a rule, and
`dbt test` runs them all and reports the failing ones. The one check that
cannot be a SELECT, because it has to switch the session timezone partway
through, is a DO block in sql/05_verify.sql that raises an exception on
failure and a notice with its numbers on success. Both kinds run every time,
so one run reports everything that is wrong.
"""

from __future__ import annotations

import logging
from contextlib import closing

import psycopg2
import psycopg2.errors

from . import database_connection, dbt_project, sql_file_runner

logger = logging.getLogger(__name__)


def run_sql_checks() -> int:
    """Run sql/05_verify.sql and return 0 if every check in it passed or 1 if
    one failed.

    Any database error other than a failed check is raised.
    """
    # autocommit: the checks write nothing that persists, and it keeps a
    # failed check from leaving the session in an aborted transaction on the
    # way out.
    with closing(database_connection.connect(autocommit=True)) as connection:
        connection.notices = sql_file_runner.NoticeLogger()
        try:
            sql_file_runner.run_sql_file(connection, sql_file_runner.VERIFY_FILE)
        except psycopg2.errors.RaiseException as error:
            # A failed check is a RAISE EXCEPTION in a DO block. str(error)
            # carries the server's CONTEXT lines, which name the block, so
            # it is printed in full before the one-line summary.
            primary_message = getattr(error.diag, "message_primary", None) or str(error)
            logger.error("full server report:\n%s", str(error).strip())
            logger.error("check FAILED: %s", primary_message)
            return 1
    return 0


def run_verify() -> int:
    """Run `dbt test` and then sql/05_verify.sql, and return 0 if every test
    and check passed or 1 if any failed.

    The second half runs even when the first fails, so one run reports
    everything that is wrong.
    """
    dbt_exit_code = dbt_project.test_warehouse()
    if dbt_exit_code != 0:
        logger.error("dbt test reported failures; they are listed above and in logs/dbt/dbt.log")
    sql_exit_code = run_sql_checks()

    if dbt_exit_code != 0 or sql_exit_code != 0:
        logger.error("verification FAILED")
        return 1
    logger.info(
        "verification passed: every dbt test passed and every check in sql/%s raised no exception",
        sql_file_runner.VERIFY_FILE,
    )
    return 0
