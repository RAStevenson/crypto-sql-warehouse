"""Render the stakeholder report from reports/metrics.json and its figures.

    python scripts/run_build_report.py

scripts/run_analysis.py runs this script itself after writing the metrics, so
a standalone run is only needed to re-render after a wording change.

Every measured quantity in the report is read from metrics.json at render time
rather than transcribed into the prose, so no number in the report can drift
from the analysis that produced it. The counts of tests, queries, and checks
are read from the files on disk. Figures are embedded as base64 data URIs, so the
HTML is one file that opens anywhere with nothing alongside it. When Chrome is
installed at its default Windows location the same HTML is printed headlessly
to PDF; when it is not, the HTML is still written and the script says so.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base64
import json
import re
import subprocess

from crypto_warehouse.ingest import DEFAULT_BACKFILL_DAYS

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS = REPO_ROOT / "reports"
METRICS = REPORTS / "metrics.json"
FIGURES = REPORTS / "figures"
OUT = REPORTS / "crypto_warehouse_report.html"
OUT_PDF = REPORTS / "crypto_warehouse_report.pdf"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

TESTS_DIR = REPO_ROOT / "tests"
SQL_DIR = REPO_ROOT / "sql"


def count_tests() -> int:
    """Return the number of test functions in tests/, counted from the files on disk."""
    return sum(
        line.lstrip().startswith("def test_")
        for path in TESTS_DIR.glob("test_*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def count_queries() -> int:
    """Return the number of numbered queries in sql/04_queries.sql, or 0 when the
    file is missing."""
    path = SQL_DIR / "04_queries.sql"
    if not path.is_file():
        return 0
    return sum(
        line.startswith("-- Q") and line[4:5].isdigit()
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def count_checks() -> int:
    """Return the number of DO blocks in sql/05_verify.sql, or 0 when the file is
    missing.

    Each block is one check and can raise on more than one condition, so this
    is a count of checks and not of RAISE statements.
    """
    path = SQL_DIR / "05_verify.sql"
    if not path.is_file():
        return 0
    return sum(
        line.startswith("DO $$") for line in path.read_text(encoding="utf-8").splitlines()
    )


# Page styles, kept as a plain string so the CSS braces need no doubling.
REPORT_CSS = """<style>
  :root {
    --ink: #1a1a1a;
    --muted: #555;
    --rule: #d4d4d4;
    --accent: #0b3d91;
    --band: #f4f6f9;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: #fff;
    color: var(--ink);
    font-family: Georgia, "Times New Roman", serif;
    font-size: 16px;
    line-height: 1.62;
  }
  article {
    max-width: 46rem;
    margin: 0 auto;
    padding: 3rem 1.5rem 4rem;
  }
  h1, h2, h3, .doctype, figcaption, caption, th, code, pre {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .titleblock {
    border-bottom: 2px solid var(--ink);
    padding-bottom: 1.5rem;
    margin-bottom: 2.5rem;
  }
  .doctype {
    text-transform: uppercase;
    letter-spacing: 0.14em;
    font-size: 0.72rem;
    color: var(--muted);
    margin: 0 0 0.75rem;
  }
  h1 { font-size: 1.85rem; line-height: 1.25; margin: 0 0 0.6rem; }
  .subtitle {
    font-size: 1.05rem;
    color: var(--muted);
    font-style: italic;
    margin: 0 0 1.5rem;
  }
  table.meta { border-collapse: collapse; font-size: 0.85rem; }
  table.meta th {
    text-align: left;
    font-weight: 600;
    color: var(--muted);
    padding: 0.15rem 1.25rem 0.15rem 0;
    white-space: nowrap;
    vertical-align: top;
  }
  table.meta td { padding: 0.15rem 0; }
  h2 {
    font-size: 1.3rem;
    margin: 2.75rem 0 1rem;
    padding-bottom: 0.35rem;
    border-bottom: 1px solid var(--rule);
  }
  h3 { font-size: 1.02rem; margin: 1.9rem 0 0.6rem; color: var(--accent); }
  p { margin: 0 0 1.1rem; }
  .callout {
    background: var(--band);
    border-left: 3px solid var(--accent);
    padding: 0.9rem 1.1rem;
    font-size: 0.94rem;
    margin-top: 1.5rem;
  }
  figure { margin: 1.9rem 0; }
  figure img {
    width: 100%;
    height: auto;
    border: 1px solid var(--rule);
    background: #fff;
  }
  figcaption {
    font-size: 0.83rem;
    color: var(--muted);
    line-height: 1.5;
    margin-top: 0.6rem;
  }
  table.data {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.86rem;
    margin: 1.6rem 0;
  }
  table.data caption {
    caption-side: top;
    text-align: left;
    font-size: 0.83rem;
    color: var(--muted);
    line-height: 1.5;
    padding-bottom: 0.6rem;
  }
  table.data th {
    text-align: left;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    border-bottom: 1.5px solid var(--ink);
    padding: 0.4rem 0.5rem;
  }
  table.data td {
    padding: 0.4rem 0.5rem;
    border-bottom: 1px solid var(--rule);
    font-variant-numeric: tabular-nums;
  }
  table.data td:not(:first-child), table.data th:not(:first-child) {
    text-align: right;
  }
  table.data tr.highlight td { background: var(--band); font-weight: 700; }
  code {
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 0.86em;
    background: var(--band);
    padding: 0.1em 0.3em;
    border-radius: 2px;
  }
  pre {
    background: var(--band);
    border-left: 3px solid var(--rule);
    padding: 0.9rem 1.1rem;
    overflow-x: auto;
    font-size: 0.84rem;
    line-height: 1.55;
  }
  pre code { background: none; padding: 0; }
  footer {
    margin-top: 3.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--rule);
    font-size: 0.8rem;
    color: var(--muted);
  }
  @media print {
    @page { margin: 20mm 18mm; }
    body { font-size: 10.5pt; }
    article { max-width: none; padding: 0; }
    h2 { page-break-after: avoid; }
    h3 { page-break-after: avoid; }
    figure, table.data, pre, .callout { page-break-inside: avoid; }
    section { page-break-inside: auto; }
    a { color: inherit; text-decoration: none; }
  }
</style>"""


def img(name: str) -> str:
    """Return a PNG from reports/figures as a base64 data URI."""
    data = base64.b64encode((FIGURES / name).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def pct(x: float, places: int = 1) -> str:
    """Return a fraction formatted as a percentage with the given decimal places."""
    return f"{100 * x:.{places}f}%"


SMALL_NUMBERS = {
    1: "One",
    2: "Two",
    3: "Three",
    4: "Four",
    5: "Five",
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
    10: "Ten",
}


def spell(n: int, capital: bool = True) -> str:
    """Return a count from 1 to 10 as a word, capitalized unless capital is
    False, and any other count as digits."""
    word = SMALL_NUMBERS.get(n, str(n))
    return word if capital else word.lower()


def listing(items: list[str], conjunction: str = "and") -> str:
    """Return the items joined by commas, with a conjunction before the last one."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return ", ".join(items[:-1]) + f", {conjunction} {items[-1]}"


def emit_pdf(html_path: Path = OUT, pdf_path: Path = OUT_PDF) -> bool:
    """Print the HTML report to PDF with headless Chrome and return True, or
    return False when Chrome is not at its default path."""
    if not CHROME.exists():
        print("Chrome not found: skipping PDF (open the HTML and print to PDF manually)")
        return False
    subprocess.run(
        [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path.resolve()}",
            html_path.resolve().as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return True


def build_report() -> None:
    """Render the HTML report from metrics.json and the figures, then print it
    to PDF when Chrome is present.

    Exits with a message when metrics.json is missing or when a figure or
    table is not cited from the body prose.
    """
    if not METRICS.exists():
        raise SystemExit(f"{METRICS} not found; run scripts/run_analysis.py first")
    m = json.loads(METRICS.read_text(encoding="utf-8"))
    panel, assets = m["panel"], m["assets"]
    corr, div, prov = m["correlation"], m["diversification"], m["provenance"]
    mart = prov["mart_correlations_90d"]

    n_tests = count_tests()
    n_queries = count_queries()
    n_checks = count_checks()
    symbols = listing(panel["symbols"])
    n_days, n_assets = panel["n_days"], panel["n_assets"]

    close_path = m["close_path"]

    # sqrt(252) is the equity convention. How far it would move these numbers
    # is arithmetic on the two day counts, not a remembered figure.
    equity_understatement = 1.0 - (252 / 365) ** 0.5

    # The least volatile single asset, for the practical comparison in
    # section 4. Read from the metrics rather than named in the prose, because
    # a reload could change which asset it is.
    least_volatile = min(assets, key=lambda a: a["annualized_volatility"])
    most_volatile = max(assets, key=lambda a: a["annualized_volatility"])
    worst_dd = min(assets, key=lambda a: a["max_drawdown"])
    vol_ratio = (
        most_volatile["annualized_volatility"] / least_volatile["annualized_volatility"]
    )

    # The depth of the decline is stated from the drawdown figures, which
    # hold whichever way the window ended: every asset fell at least this far
    # below its own peak. That the falls happened together is what Figure 3
    # shows, and the prose cites the figure for it.
    shallowest_drawdown = -max(a["max_drawdown"] for a in assets)

    # Some sentences state qualitative facts as literals that no placeholder
    # can update, and they hold for the recorded run. Each one computable from
    # the metrics has a condition below, with a rough threshold, and a reload
    # that breaks one prints a warning naming the sentence to reword. It is a
    # warning and not a failed build because this builder also renders other
    # people's loads, so such a reload still writes a report with the false
    # sentence in it, body prose included, until someone rewords it. Four
    # literals describe the shape of Figure 3 and cannot be computed from the
    # metrics ("fall together and rebound together", "mostly depth rather than
    # timing", "falling together" in section 1, "one deep joint decline" in
    # section 5), so check them against the figure by eye after a reload.
    limit_gap = (
        div["equal_weight_portfolio_vol"] - div["equicorrelation_limit_vol"]
    ) / div["equal_weight_portfolio_vol"]
    literal_conditions = {
        "every asset fell at least 10% below its peak (section 1 selloff, "
        "section 5 decline)": shallowest_drawdown >= 0.10,
        "every correlation is positive (section 2.4, Table 1 positive)":
            corr["off_diagonal_min"] > 0,
        "correlations sit in one high block (section 3.1 no pair to lean on, "
        "Figure 1 caption one block)": corr["off_diagonal_min"] >= 0.5,
        "the basket captured under a quarter of the available reduction "
        "(section 4 close to useless, Figure 4 close to the left bar)":
            div["diversification_captured"] < 0.25,
        "the most volatile asset has at least 1.5 times the least volatile's "
        "volatility (Figure 2 not interchangeable, section 5 risk parity "
        "probably lower)": vol_ratio >= 1.5,
        "the equicorrelation limit is within 5% of the basket (section 4 "
        "another name would buy very little)": limit_gap < 0.05,
    }

    # Section 4 compares the least volatile asset with the basket. Compare at
    # the precision printed, adding places until the two values differ, so the
    # sentence never prints two equal numbers as one above the other.
    lv, ew = least_volatile["annualized_volatility"], div["equal_weight_portfolio_vol"]
    places = 3
    while round(lv, places) == round(ew, places) and places < 6:
        places += 1
    if round(lv, places) < round(ew, places):
        least_volatile_paragraph = f"""<p>The sharper version of the same point is in Table 2.
{least_volatile["symbol"]} alone had a volatility of {lv:.{places}f},
which is below the equal-weight basket's {ew:.{places}f}. Over this window,
spreading into the other {n_assets - 1} names raised volatility rather than
lowering it, because the diversification benefit was smaller than the extra
variance those names brought with them. Equal weight assumes the assets are
interchangeable, and on this evidence they are not.</p>"""
    elif round(lv, places) > round(ew, places):
        least_volatile_paragraph = f"""<p>Table 2 gives the per-asset figures. The least
volatile asset, {least_volatile["symbol"]}, had a volatility of
{lv:.{places}f}, above the equal-weight basket's {ew:.{places}f}. Over this
window, spreading into the other {n_assets - 1} names lowered volatility below
even the least volatile asset, by
{round(lv, places) - round(ew, places):.{places}f}.</p>"""
    else:
        least_volatile_paragraph = f"""<p>Table 2 gives the per-asset figures. The least
volatile asset, {least_volatile["symbol"]}, had a volatility of
{lv:.{places}f}, the same as the equal-weight basket to {places} decimal
places. Over this window, spreading into the other {n_assets - 1} names
neither raised nor lowered volatility relative to it.</p>"""

    shared_days_phrase = (
        f"{mart['n_days_min']} to {mart['n_days_max']}"
        if mart["n_days_min"] != mart["n_days_max"]
        else f"{mart['n_days_min']}"
    )

    # How the uncertainty on one correlation compares with the whole spread
    # across pairs. Computed rather than asserted, because which of the two is
    # larger decides whether the heatmap's ordering means anything.
    ci_width = corr["mean_ci95_high"] - corr["mean_ci95_low"]
    pair_spread = corr["off_diagonal_max"] - corr["off_diagonal_min"]
    ci_vs_spread = ci_width / pair_spread
    # Compared at the precision the ratio is printed at, so "about 100% as
    # wide" never sits beside the wording for a ratio below 1.
    if round(ci_vs_spread, 2) >= 1:
        ordering_caveat = """the ordering of individual pairs in the
correlation matrix is not established by this sample"""
        ordering_finding = """The ordering of pairs in that figure is therefore not
established by this sample. What survives is that the whole block sits high,
not that any particular pair sits above any other."""
    else:
        ordering_caveat = """the ordering of individual pairs in the
correlation matrix is supported only between pairs that sit far apart"""
        ordering_finding = f"""Two pairs' intervals stop overlapping only when
their correlations differ by more than about {ci_width:.3f}, so this check
supports the figure's ordering only between pairs that far apart. Beyond that, what survives is
that the whole block sits high."""

    def verb(n: int, singular: str, plural: str) -> str:
        """Return the singular wording when n is 1 and the plural otherwise."""
        return singular if n == 1 else plural

    # Why each excluded date is out, read from the analysis rather than
    # inferred by subtraction, so the sentence stays true when an asset joins
    # late or stops early.
    partial_dates = prov["incomplete_dates"]
    n_unshared = len(prov["dates_without_every_asset"])
    n_after = len(prov["dates_after_an_excluded_day"])
    n_dropped = prov["dates_dropped_from_panel"]
    # Each reason as (count, what is true of those dates). The last one is
    # worded to hold for every date the analysis files under it: a complete
    # day whose previous day is not a complete one, including the first day
    # of history.
    exclusion_reasons = [
        (
            len(partial_dates),
            verb(len(partial_dates), "has", "have")
            + f" at least one partial bar ({listing(partial_dates)})",
        ),
        (
            n_unshared,
            verb(n_unshared, "is a date", "are dates")
            + " on which not every asset has a daily bar",
        ),
        (
            n_after,
            verb(
                n_after,
                "is a complete day with no complete day directly before it, so it has",
                "are complete days with no complete day directly before them, so they have",
            )
            + " no one-day return (section 2.2)",
        ),
    ]
    exclusion_reasons = [(n, text) for n, text in exclusion_reasons if n]
    excluded_count = (
        f"Of {prov['observed_dates']} observed dates, {n_dropped} "
        f"{verb(n_dropped, 'is', 'are')} excluded"
    )
    # True only when this run's history is shorter than a default first load,
    # so the sentence is left out of a report rendered from a full one.
    fresh_load_sentence = (
        f" A fresh load reaches back {DEFAULT_BACKFILL_DAYS} days, further than the "
        "history behind this run, so its panel starts earlier as well."
        if prov["observed_dates"] < DEFAULT_BACKFILL_DAYS
        else ""
    )

    if not exclusion_reasons:
        exclusion_sentence = f"All {prov['observed_dates']} observed dates are in the panel."
    elif len(exclusion_reasons) == 1:
        # One reason covers every excluded date, so the count is not repeated.
        exclusion_sentence = (
            f"{excluded_count}. {verb(n_dropped, 'It', 'They')} {exclusion_reasons[0][1]}."
        )
    else:
        exclusion_sentence = (
            f"{excluded_count}: "
            + listing([f"{n} {text}" for n, text in exclusion_reasons])
            + "."
        )

    asset_rows = "\n".join(
        f"<tr><td>{a['symbol']}</td><td>{a['annualized_volatility']:.3f}</td>"
        f"<td>{pct(a['max_drawdown'])}</td><td>{a['total_log_return']:+.3f}</td></tr>"
        for a in sorted(assets, key=lambda a: -a["annualized_volatility"])
    )

    corr_header = "".join(f"<th>{s}</th>" for s in corr["symbols"])
    corr_rows = "\n".join(
        f"<tr><td>{sym}</td>"
        + "".join(
            f"<td>{'1.00' if i == j else f'{value:.3f}'}</td>"
            for j, value in enumerate(corr["matrix"][i])
        )
        + "</tr>"
        for i, sym in enumerate(corr["symbols"])
    )

    html = f"""<article>
<header class="titleblock">
  <p class="doctype">Analytical Report</p>
  <h1>Diversification Across {spell(n_assets)} Major Crypto Assets</h1>
  <p class="subtitle">How much volatility an equal-weight holding actually
  removes, measured over {n_days} complete trading days</p>
  <table class="meta">
    <tr><th>Author</th><td>Robert Stevenson</td></tr>
    <tr><th>Repository</th><td>crypto-sql-warehouse</td></tr>
    <tr><th>Panel</th><td>{n_days} complete trading days, {panel["first_date"]} to
      {panel["last_date"]}, {n_assets} assets</td></tr>
    <tr><th>Source</th><td><code>marts.daily_metrics</code> on PostgreSQL
      {prov["server_version"]}</td></tr>
    <tr><th>Source of figures</th><td><code>reports/metrics.json</code>, regenerated by
      <code>python scripts/run_analysis.py</code></td></tr>
  </table>
</header>

<section>
<h2>1. Question and why it matters</h2>

<p>Holding several assets instead of one reduces risk only to the extent that
those assets move independently. This report measures how much of that
reduction {spell(n_assets, capital=False)} major crypto assets actually
deliver. The assets are {symbols}, all USD spot pairs from a single exchange.
The portfolio holds them in equal weight, {pct(div["weight_per_asset"], 2)}
each.</p>

<p>The reason to measure it rather than assume it is that the usual case for
holding several coins rests on them being different assets with different
drivers. If their daily returns move together, an equal-weight basket is close
to a single position spread across {n_assets} tickers, and the extra fees and
operational surface of {n_assets} holdings buy very little risk reduction.</p>

<p class="callout">Every volatility, correlation, and diversification figure
in this report is computed on one basis, the {n_days}-day panel of days where
all {n_assets} assets have a clean daily return. Drawdown and total return
are computed from the daily closes over the same window, as Table 2 states. Section 2
states how the panel is built and section 5 states what it cannot support.</p>

<p>On this panel the equal-weight portfolio has an annualized volatility of
{div["equal_weight_portfolio_vol"]:.4f}, against
{div["average_single_asset_vol"]:.4f} for the average single asset. That is
{pct(div["risk_removed_actual"])} of the volatility removed. If the same
{n_assets} assets had kept their variances with zero correlation between
them, the portfolio would have come out at
{div["zero_correlation_vol"]:.4f}, removing
{pct(div["risk_removed_if_uncorrelated"])}. The portfolio captures
{pct(div["diversification_captured"])} of the reduction that independence
between the same {n_assets} assets would have given.</p>

<p>Two caveats belong with that number rather than after it. The panel is
{n_days} days, short enough that {ordering_caveat}. And Figure 3 shows all
{n_assets} assets falling together, each at least
{pct(shallowest_drawdown)} below its peak, so the
{pct(div["risk_removed_actual"])} is measured partly through a joint selloff,
which is when correlations are widely reported to run highest. Section 5
gives both in full.</p>
</section>

<section>
<h2>2. Data and method</h2>

<h3>2.1 Source and grain</h3>

<p>Every number comes from <code>marts.daily_metrics</code> in the warehouse
this repository builds. That mart holds one row per asset per UTC trading day,
rolled up in SQL from {prov["ohlcv_hourly_rows"]:,} hourly candles (the open,
high, low, close, and volume for each hour) in <code>core.ohlcv_hourly</code>
into {prov["daily_ohlcv_rows"]:,} daily bars in
<code>marts.daily_ohlcv</code>. Daily bars are derived from hourly ones rather
than fetched separately, so any disagreement between the two granularities can
only be a rollup bug, which is findable, and never a source discrepancy, which
is not.</p>

<p>The day boundary is pinned to UTC in the rollup expression rather than left
to the session timezone. A plain cast would rebucket the whole mart for a
client connecting from another zone, which is a bug this warehouse has already
had once and now has a regression test for. The verify suite re-runs the
mart's own defining query under America/Chicago and again under UTC and fails
if any row differs, and the database default of UTC stays as a second layer.</p>

<h3>2.2 The completeness rule, and why partial days are excluded</h3>

<p>The loaded window starts and ends partway through a day, and the exchange
has real outages, so some asset-days are built from fewer than 24 hourly
candles. {prov["incomplete_asset_days"]} of the
{prov["daily_ohlcv_rows"]:,} daily rows are partial in that way.</p>

<p>A partial day is a different measurement, not a smaller one. Its close is
whatever the last observed hour printed rather than the 23:00 candle, and its
volume is the sum of however many hours happened to land. Whether it is an up
day or a down day depends on when the loader ran rather than on the
market. Any variance or correlation computed over such a bar inherits that
distortion. The mart therefore keeps the observed open, high, low, close, and
volume on those days and
sets every derived column, including <code>log_return</code>, to NULL. On any
complete day with no complete day directly before it, the mart sets the
return to NULL as well, because a return computed across a gap covers more than one day
while still labeled as one day.</p>

<h3>2.3 Building one panel</h3>

<p>A covariance matrix has to describe one portfolio over one set of dates, so
the panel keeps only dates where all {n_assets} assets have a non-null log
return. {exclusion_sentence} Each exclusion removes
the date for all {n_assets} assets at once, so the panel is not lopsided toward
any name. What remains is {n_days} days from {panel["first_date"]} to
{panel["last_date"]}, {n_days * n_assets:,} asset-days with no holes.</p>

<p>The alternative, computing each correlation over whatever dates that
particular pair happened to share, lets different entries of the matrix
describe different periods. A matrix assembled that way is not guaranteed to be
positive semidefinite, and the portfolio variance it produces can come out
negative. Dropping {n_dropped} {verb(n_dropped, "date", "dates")} for everyone is the
cheaper problem.</p>

<h3>2.4 Returns, annualization, and covariance</h3>

<p>Returns are logarithmic, ln(close today / close yesterday), because they add
across periods and are the standard input to a variance estimate. Volatility is
the sample standard deviation of those daily returns with the n minus 1
divisor, multiplied by the square root of 365. Volatility is stated per year
because that is how it is conventionally quoted, which lets these figures be
compared with published ones.</p>

<p>The 365 is deliberate. Crypto trades every calendar day including weekends
and holidays, so a year holds 365 return observations. The square root of 252
that equity work uses counts exchange sessions, and a market with no sessions
off does not have 252 of them. Annualizing this data on 252 would report every
volatility in this report about {pct(equity_understatement, 0)} lower than it
is.</p>

<p>The covariance matrix is the sample covariance of the same daily log
returns, also with the n minus 1 divisor, scaled by 365. Portfolio volatility
is the square root of w'Cw, the weight vector times the covariance matrix
times the weight vector, with w set to {div["weight_per_asset"]:.4f} in every
position. The zero-correlation counterfactual replaces C with its own diagonal.
Every asset keeps its variance exactly, and every covariance is set to zero.
Negative covariances put a portfolio below that counterfactual, which is what
a hedge is for, and <code>tests/test_analysis.py</code>
checks that the arithmetic behaves that way. Every correlation in Table 1 is
positive, so for the case at hand the zero-correlation portfolio is the
reference the real number is measured against.</p>

<h3>2.5 One basis, stated once</h3>

<p>The warehouse also publishes <code>marts.pairwise_correlations_90d</code>,
which reports {mart["min"]:.3f} to {mart["max"]:.3f} across its
{mart["n_pairs"]} pairs. That mart runs on a trailing 90-day window and has
{shared_days_phrase} shared days per pair, so it is a
different window on the same data
and its numbers are not interchangeable with the ones below. Nothing in this
report uses them.</p>
</section>

<section>
<h2>3. Results</h2>

<h3>3.1 How closely the assets move together</h3>

<p>All {corr["n_pairs"]} pairs sit between {corr["off_diagonal_min"]:.3f} and
{corr["off_diagonal_max"]:.3f}, averaging {corr["off_diagonal_mean"]:.3f}. The
least correlated pair is {listing(corr["least_correlated_pair"])} at
{corr["off_diagonal_min"]:.3f} and the most correlated is
{listing(corr["most_correlated_pair"])} at {corr["off_diagonal_max"]:.3f}. There is no
pair in this set that a portfolio could lean on for independence. Figure 1
shows the matrix as a heatmap, and Table 1 gives its values.</p>

<figure>
  <img src="{img("correlation_heatmap.png")}"
       alt="Pairwise correlation heatmap for the {n_assets} assets">
  <figcaption><strong>Figure 1.</strong> Pairwise correlation of daily log
  returns over the {n_days}-day panel. The color axis runs from
  {corr["heatmap_scale_low"]:.2f} to {corr["heatmap_scale_high"]:.2f}
  rather than the full -1 to 1 range, because on a full scale every cell here
  would render in nearly the same tone. Read the colors as differences within
  a tight band, not as a general correlation scale. The diagonal is grayed out
  and left off the color scale, since a variable's correlation with itself is 1
  by construction. The assets
  form one block, and the visible variation runs from
  {corr["off_diagonal_min"]:.3f} to {corr["off_diagonal_max"]:.3f}, which is a
  band inside the high end rather than a range from low to high.</figcaption>
</figure>

<table class="data">
  <caption><strong>Table 1.</strong> The full correlation matrix behind Figure
  1, on the {n_days}-day complete-day panel.</caption>
  <thead><tr><th></th>{corr_header}</tr></thead>
  <tbody>{corr_rows}</tbody>
</table>

<h3>3.2 How volatile the assets are individually</h3>

<p>Annualized volatility ranges from {least_volatile["annualized_volatility"]:.3f}
for {least_volatile["symbol"]} to {most_volatile["annualized_volatility"]:.3f} for
{most_volatile["symbol"]}, averaging {div["average_single_asset_vol"]:.3f}. Figure 2
shows each asset against that average. The average is the reference the
portfolio has to beat, because it is what an investor holding one of these
names at random would have had.</p>

<figure>
  <img src="{img("asset_volatility.png")}" alt="Annualized volatility by asset, sorted">
  <figcaption><strong>Figure 2.</strong> Annualized volatility per asset,
  sorted, with the {div["average_single_asset_vol"]:.3f} average marked. The
  assets are not interchangeable in risk terms.
  {most_volatile["symbol"]} has roughly {vol_ratio:.1f} times the volatility of
  {least_volatile["symbol"]}, so equal weight is not the same thing as equal risk
  contribution.</figcaption>
</figure>

<table class="data">
  <caption><strong>Table 2.</strong> Per-asset figures. Volatility is computed
  from the panel's returns. Max drawdown and total log return are computed
  from the closes of every complete day from {close_path["first_date"]} to
  {close_path["last_date"]}, so they include the moves on days the panel
  excludes. The first of those closes is the one the panel's first return is
  measured from. Drawdown is measured from the running peak within that window,
  not from an all-time high.</caption>
  <thead><tr><th>Asset</th><th>Annualized volatility</th><th>Max drawdown</th>
    <th>Total log return</th></tr></thead>
  <tbody>{asset_rows}</tbody>
</table>

<h3>3.3 What the co-movement looks like over time</h3>

<p>The correlation matrix states the co-movement as {corr["n_pairs"]} numbers.
The drawdown paths in Figure 3 are consistent with it. The assets fall
together and rebound together, and the differences between them are mostly depth rather
than timing.</p>

<figure>
  <img src="{img("drawdown_curves.png")}"
       alt="Drawdown from running peak for all {n_assets} assets">
  <figcaption><strong>Figure 3.</strong> Each asset's drawdown from its own
  running peak, all on one axis. The assets fall together and rebound
  together. The deepest drawdown is
  {pct(worst_dd["max_drawdown"])}, for {worst_dd["symbol"]}.</figcaption>
</figure>

<h3>3.4 The diversification result</h3>

<p>Equal weights on the {n_days}-day covariance matrix give an annualized
portfolio volatility of {div["equal_weight_portfolio_vol"]:.4f}. The average
single asset had {div["average_single_asset_vol"]:.4f}, so the basket
removed {pct(div["risk_removed_actual"])} of the volatility. Holding the same
{n_assets} variances with every correlation set to zero would have given
{div["zero_correlation_vol"]:.4f}, a reduction of
{pct(div["risk_removed_if_uncorrelated"])}. The ratio of the two,
{pct(div["diversification_captured"])}, is the share of the available
diversification these assets actually delivered. Figure 4 puts the three
volatilities side by side, and Table 3 lists them with the two reductions and
their ratio.</p>

<figure>
  <img src="{img("diversification.png")}"
       alt="Average single-asset, equal-weight portfolio, and zero-correlation volatility">
  <figcaption><strong>Figure 4.</strong> Three annualized volatilities: on the
  left the average of holding one of these assets, in the middle the
  equal-weight portfolio, on the right the same {n_assets} variances with zero
  correlation. The left-to-middle gap is the benefit received and the
  middle-to-right gap is what the correlations consumed. Read the position
  of the middle bar. It sits close to the left one and far from the
  right one.</figcaption>
</figure>

<table class="data">
  <caption><strong>Table 3.</strong> The three volatilities, all annualized,
  with the two reductions and their ratio.</caption>
  <thead><tr><th>Quantity</th><th>Value</th></tr></thead>
  <tbody>
    <tr><td>Average single-asset volatility</td>
      <td>{div["average_single_asset_vol"]:.4f}</td></tr>
    <tr class="highlight"><td>Equal-weight portfolio volatility</td>
      <td>{div["equal_weight_portfolio_vol"]:.4f}</td></tr>
    <tr><td>Same variances, zero correlation</td>
      <td>{div["zero_correlation_vol"]:.4f}</td></tr>
    <tr><td>Risk removed by the portfolio</td>
      <td>{pct(div["risk_removed_actual"])}</td></tr>
    <tr><td>Risk removable at zero correlation</td>
      <td>{pct(div["risk_removed_if_uncorrelated"])}</td></tr>
    <tr class="highlight"><td>Share of available diversification captured</td>
      <td>{pct(div["diversification_captured"])}</td></tr>
  </tbody>
</table>
</section>

<section>
<h2>4. What this means in practice</h2>

<p>Spreading a holding across these {n_assets} names removed
{pct(div["risk_removed_actual"])} of its volatility. Counting names is
therefore close to useless as a risk control here. Independence between the
same {n_assets} assets would have removed
{pct(div["risk_removed_if_uncorrelated"])}, so the shortfall sits in the
correlations and not in the number or the size of the positions. The
actual correlations deliver {pct(div["diversification_captured"])} of that
available reduction.
An equal-weight basket of equally correlated assets converges toward the
average volatility times the square root of the mean correlation. That limit is
{div["equicorrelation_limit_vol"]:.3f} here against the portfolio's
{div["equal_weight_portfolio_vol"]:.3f}, so another name from the same group
would buy very little.</p>

{least_volatile_paragraph}

<p>Two consequences follow for anyone using this warehouse. First, position
size and hedging do the work that name count does not. At a mean correlation
of {corr["off_diagonal_mean"]:.3f}, total exposure is the variable that moves
portfolio risk. Second, the quantity worth monitoring is the correlation level
itself. <code>marts.pairwise_correlations_90d</code> recomputes it over the
trailing 90 days at every refresh and keeps no history, so following it over
time means saving a copy after each refresh. Only a fall in that correlation would change the
conclusion here.</p>

<p>None of this is an argument about which assets to hold. It measures what the
{n_assets} names already loaded did over one window. Section 5 states what that
window does not cover.</p>
</section>

<section>
<h2>5. Limitations</h2>

<p>The panel is {n_days} days. A single pair at the average
correlation of {corr["off_diagonal_mean"]:.3f} has a 95% Fisher interval of
{corr["mean_ci95_low"]:.3f} to {corr["mean_ci95_high"]:.3f} on this many
observations, a width of {ci_width:.3f}. Fisher's method keeps the interval
inside -1 to 1 and accurate for correlations as high as these. The entire spread between the
least and most correlated pair in Figure 1 is {pair_spread:.3f}, so the
uncertainty on any one correlation is about {pct(ci_vs_spread, 0)} as wide as
the range the heatmap is showing. {ordering_finding}</p>

<p>The data covers one exchange and one quote currency. All candles come from
Coinbase and every pair is quoted in USD. There is no second venue to check the
prices against, so a venue-specific outage or a stale feed enters the
covariance matrix as if it were market structure. Common exposure to the dollar
sits inside every one of these correlations, and with no second quote currency
loaded there is no way to separate it out.</p>

<p>Correlations are not stable, and they are widely reported to rise in
selloffs. This is the limitation that matters most for how the diversification
result should be read, because a selloff is when diversification is being
asked to do something. This panel cannot test that pattern on its own. It
holds one deep joint decline, visible in Figure 3, in which every asset fell at
least {pct(shallowest_drawdown)} below its peak, and one such decline is too little
to measure how correlation inside a selloff differs from correlation outside
one. The {pct(div["risk_removed_actual"])} is therefore a reading taken partly
through a stressed market. If correlations do rise in selloffs, the days
outside the decline lowered the measured correlation below its selloff
level, so the
{pct(div["risk_removed_actual"])} is more likely to be optimistic than conservative
as a forecast for the next selloff.</p>

<p>Drawdown is measured only over the days this report covers.
The peak in Table 2 and Figure 3 is the highest close since
{close_path["first_date"]}, not an all-time high. Backfilling earlier history
could change any drawdown figure in this report. These are not the numbers a
trading desk means by maximum drawdown.</p>

<p>Equal weight is not an optimized portfolio. The weights are
{div["weight_per_asset"]:.4f} each because that is the simplest allocation to
state, not because anything here selected them. A minimum-variance allocation
cannot have a higher portfolio volatility than equal weight on this panel,
because it is chosen to minimize exactly that. Given the {vol_ratio:.1f} to 1
spread in single-asset volatility, a risk-parity allocation would probably
have a lower one too.
The w'Cw calculation also assumes the weights stay at
{div["weight_per_asset"]:.4f}, which in practice needs rebalancing, and no
transaction costs, funding, borrow, or slippage are modeled anywhere in this
report.</p>

<p>{spell(n_assets)} assets is not the market. These are large-cap
USD spot pairs, which is one corner of one asset class. I picked them for
coverage and liquidity when I built the warehouse. They are not a sample of
anything. A result about {symbols} is a result about those {n_assets} names on
this venue over these {n_days} days, and reading it as a claim about crypto in
general would be reading it too widely.</p>
</section>

<section>
<h2>6. How to reproduce</h2>

<p>Reproduction needs Python 3.11 or 3.12 and a PostgreSQL server you can
connect to. This run used PostgreSQL
{prov["server_version"]}. Nothing in the SQL needs a feature newer than
PostgreSQL 10. From the repository root (on Mac or Linux, use
<code>cp</code> in place of <code>copy</code>):</p>

<pre><code>pip install -r requirements.txt
copy .env.example .env
python scripts/run_setup.py
python scripts/run_ingest.py
python scripts/run_analysis.py</code></pre>

<p>Between the copy and <code>run_setup.py</code>, edit <code>DATABASE_URL</code>
in <code>.env</code> to point at your server. <code>run_setup.py</code> creates
the database and applies the schema and
the marts. <code>run_ingest.py</code> backfills hourly candles and refreshes
the marts. The first run pulls the full backfill and every later run resumes
from the watermark, the last hour already loaded. <code>run_analysis.py</code>
builds the panel, writes <code>reports/metrics.json</code> and the four figures
under <code>reports/figures/</code>, prints the main results to the log, and
then renders this document. When Chrome is installed at its default Windows
location, the render also prints the document to PDF.
<code>run_build_report.py</code> repeats only the render, for a wording change
that needs no new numbers. Every measured quantity in the prose above is read
from <code>reports/metrics.json</code> at render time, so no number here can
drift from the analysis that produced it. The fixed conventions
the method rests on, the 365-day year and the 24-hour day, are written into the
template and stated in section 2.</p>

<p>The pipeline underneath is checked three ways. <code>python
scripts/run_verify.py</code> runs {n_checks} checks against a loaded warehouse,
each fatal on failure, covering hourly gaps, rollup coverage, raw-to-core
agreement, upsert idempotency, run status, session-timezone invariance of
the daily rollup, and agreement of the two stored daily marts with their
definitions.
<code>pytest</code> runs {n_tests} test functions, none of which need a
database or a network. They cover the ingestion windowing and watermark, the
API client, asset registration, and the diversification math in this report,
and one checks that every function is documented. The
{n_queries} annotated queries in <code>sql/04_queries.sql</code> each run
standalone against a loaded database.</p>

<p>Rerunning the ingestion loads whatever the exchange has published since,
which moves the panel's end date and every number here.{fresh_load_sentence} The figures in this
document are the ones produced by the run recorded in
<code>reports/metrics.json</code>, generated {m["generated_at_utc"]} against
PostgreSQL {prov["server_version"]}.</p>
</section>

<footer>
<p>Generated from <code>reports/metrics.json</code> by
<code>scripts/run_build_report.py</code>. Panel {panel["first_date"]} to
{panel["last_date"]}, {n_days} complete days, {n_assets} assets,
{n_days * n_assets:,} asset-days.</p>
</footer>
</article>

{REPORT_CSS}
"""

    # A figure or table that no body prose points at ships without saying
    # what it is evidence for, so an uncited one fails the build instead of
    # waiting for a reviewer. Figure and table captions are stripped before
    # counting, so a mention in a caption does not count as a citation.
    body = re.sub(r"<(fig)?caption>.*?</(fig)?caption>", "", html, flags=re.S)
    uncited = [
        f"{kind} {n}"
        for kind, n in re.findall(r"<strong>(Figure|Table) (\d+)\.</strong>", html)
        if not re.search(rf"\b{kind} {n}\b", body)
    ]
    if uncited:
        raise SystemExit("not cited from body prose: " + ", ".join(uncited))

    for condition, holds in literal_conditions.items():
        if not holds:
            print(f"warning: no longer true on this data, reword by hand: {condition}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, self-contained)")
    if emit_pdf():
        print(f"wrote {OUT_PDF} ({OUT_PDF.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    build_report()
