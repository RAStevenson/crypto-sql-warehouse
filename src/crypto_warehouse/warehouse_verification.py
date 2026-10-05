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

from . import database_connection, sql_file_runner

log = logging.getLogger(__name__)


def run_verify() -> int:
    """Run sql/05_verify.sql and return 0 if it completed or 1 if a check or any
    other statement raised a database error."""
    # autocommit: the checks write nothing that persists, and it keeps a
    # failed check from leaving the session in an aborted transaction on the
    # way out.
    with closing(database_connection.connect(autocommit=True)) as conn:
        conn.notices = sql_file_runner.NoticeLogger()
        try:
            sql_file_runner.run_sql_file(conn, sql_file_runner.VERIFY_FILE)
        except psycopg2.Error as exc:
            # message_primary is the RAISE EXCEPTION text; str(exc) carries the
            # CONTEXT lines too, which say which DO block raised.
            primary = getattr(exc.diag, "message_primary", None) or str(exc)
            log.error("verification FAILED: %s", primary)
            log.error("full server report:\n%s", str(exc).strip())
            return 1

    log.info(
        "verification passed: every check in sql/%s raised no exception",
        sql_file_runner.VERIFY_FILE,
    )
    return 0
