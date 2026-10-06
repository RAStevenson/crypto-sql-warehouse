"""Running sql/05_verify.sql and turning its result into an exit code.

The checks are DO blocks that RAISE EXCEPTION on failure and RAISE NOTICE with
their numbers on success. Both go to the client as server messages, so the job
here is to make sure the operator actually sees them: notices are logged as
they arrive, and a failure is reported with the exception's own message, which
is the text that names the check that failed.
"""

from __future__ import annotations

import logging
from contextlib import closing

import psycopg2
import psycopg2.errors

from . import database_connection, sql_file_runner

logger = logging.getLogger(__name__)


def run_verify() -> int:
    """Run sql/05_verify.sql and return 0 if every check passed or 1 if one
    failed.

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
            logger.error(
                "verification FAILED: %s; the checks after it in sql/%s did not run",
                primary_message,
                sql_file_runner.VERIFY_FILE,
            )
            return 1

    logger.info(
        "verification passed: every check in sql/%s raised no exception",
        sql_file_runner.VERIFY_FILE,
    )
    return 0
