"""Writing a slice of the warehouse to a CSV file.

Two exports. A price export is the stored candles for chosen assets at one
timeframe over a date range, from core.ohlcv. A daily export is the rows of
marts.tableau_daily_extract for chosen assets over a date range: daily
prices with returns, moving averages, volatility, and drawdown.

The rows go through COPY ... TO STDOUT, which streams them from the server
straight into the file, so a five-year export of 5-minute candles is never
held in memory, and every price arrives as the exact decimal text Postgres
stores rather than a float that Python formatted.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .database_connection import REPO_ROOT, OperatorError

logger = logging.getLogger(__name__)

EXPORT_DIR = REPO_ROOT / "exports"

# Times are written as UTC wall-clock values with no offset. ts_utc AT TIME
# ZONE 'UTC' turns the stored instant into a plain timestamp, so the file
# reads the same whatever timezone the connecting session uses, the same rule
# every query in sql/ follows.
PRICES_QUERY = """
SELECT
    a.symbol,
    a.product_code,
    t.timeframe_code,
    o.ts_utc AT TIME ZONE 'UTC' AS time_utc,
    o.open,
    o.high,
    o.low,
    o.close,
    o.volume
FROM core.ohlcv o
JOIN core.assets a USING (asset_id)
JOIN core.timeframes t USING (granularity_seconds)
WHERE t.timeframe_code = %(timeframe_code)s
  AND a.product_code = ANY(%(product_codes)s)
  AND (%(start)s::timestamptz IS NULL OR o.ts_utc >= %(start)s::timestamptz)
  AND (%(end)s::timestamptz IS NULL OR o.ts_utc < %(end)s::timestamptz)
ORDER BY a.symbol, o.ts_utc
"""

DAILY_QUERY = """
SELECT e.*
FROM marts.tableau_daily_extract e
JOIN core.assets a ON a.symbol = e.symbol
WHERE a.product_code = ANY(%(product_codes)s)
  AND (%(start_date)s::date IS NULL OR e.trade_date >= %(start_date)s::date)
  AND (%(end_date)s::date IS NULL OR e.trade_date <= %(end_date)s::date)
ORDER BY e.symbol, e.trade_date
"""


def resolve_export_products(cursor, requested: list[str] | None) -> list[str]:
    """Return the sorted product codes to export, which are the requested
    codes or, with none requested, every active asset.

    A requested code missing from core.assets raises OperatorError.
    """
    if requested:
        cursor.execute(
            "SELECT product_code FROM core.assets WHERE product_code = ANY(%s)",
            (list(requested),),
        )
        found = {row[0] for row in cursor.fetchall()}
        missing = sorted(set(requested) - found)
        if missing:
            raise OperatorError(f"not in core.assets: {missing}")
        return sorted(found)
    cursor.execute("SELECT product_code FROM core.assets WHERE is_active ORDER BY product_code")
    return [row[0] for row in cursor.fetchall()]


def check_timeframe(cursor, timeframe_code: str) -> None:
    """Raise OperatorError when timeframe_code is not a row of core.timeframes,
    naming the codes that are."""
    cursor.execute("SELECT timeframe_code FROM core.timeframes ORDER BY granularity_seconds")
    known = [row[0] for row in cursor.fetchall()]
    if timeframe_code not in known:
        raise OperatorError(f"unknown timeframe {timeframe_code!r}; known timeframes: {known}")


def day_bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """Return the inclusive date range as a half-open UTC range of instants,
    from the start of the first day to the start of the day after the last."""
    start_instant = datetime.combine(start, time(0), tzinfo=UTC) if start else None
    end_instant = datetime.combine(end + timedelta(days=1), time(0), tzinfo=UTC) if end else None
    return start_instant, end_instant


def default_output_path(
    kind: str,
    product_codes: list[str],
    timeframe_code: str | None,
    start: date | None,
    end: date | None,
) -> Path:
    """Return exports/<name>.csv, with a name built from what the export holds,
    such as prices_BTC-USD_15m_2024-01-01_to_2024-06-30.csv."""
    assets = product_codes[0] if len(product_codes) == 1 else f"{len(product_codes)}-assets"
    parts = [kind, assets]
    if timeframe_code:
        parts.append(timeframe_code)
    first_day = start.isoformat() if start else "first"
    last_day = end.isoformat() if end else "last"
    parts.append(f"{first_day}_to_{last_day}")
    return EXPORT_DIR / ("_".join(parts) + ".csv")


def write_csv(connection, query: str, query_parameters: dict, output_path: Path) -> int:
    """Run query with its parameters, write the result with a header row to
    output_path, and return the number of data rows written.

    The parent directory is created when missing.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with connection.cursor() as cursor:
        # COPY takes no bind parameters, so the values are quoted into the
        # statement by psycopg2's own escaping before it is wrapped.
        bound_query = cursor.mogrify(query, query_parameters).decode()
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            cursor.copy_expert(f"COPY ({bound_query}) TO STDOUT WITH (FORMAT csv, HEADER)", handle)
        return cursor.rowcount
