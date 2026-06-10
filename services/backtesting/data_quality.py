"""Data-quality checks for the QuantEmbrace backtesting lab historical lake.

Runs the NSE OHLCV quality checks defined in
``docs/backtesting/aws-data-lake-contract.md`` over a normalised candle frame
(see ``data_loader.load_candles``), returning a structured result and a markdown
renderer for ``reports/data-quality/``.

Checks: missing candles · duplicate timestamps · invalid OHLC · zero/negative
prices · zero volume · outlier jumps · market-hours violations · missing trading
days · symbol-mapping gaps · corporate-action gaps · timezone errors · future
timestamps.

Pure functions over a pandas DataFrame — no I/O, no broker APIs. ERROR-severity
issues fail the dataset; a LOW-trust (quarantined) dataset is never eligible for
strategy validation or model training regardless of check outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from enum import Enum
from typing import Any

import pandas as pd

from backtesting.s3_data_catalog import TrustLevel, is_quarantined

IST = "Asia/Kolkata"

# NSE continuous session (IST).
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Expected candles per trading day per interval (continuous session = 375 min).
BARS_PER_DAY: dict[str, int] = {"1m": 375, "5m": 75, "15m": 25, "1d": 1}

DEFAULT_OUTLIER_THRESHOLD = 0.20  # 20% bar-to-bar move
DEFAULT_CA_GAP_THRESHOLD = 0.20   # 20% overnight gap ⇒ suspected corp-action


class Severity(str, Enum):
    ERROR = "ERROR"
    WARN = "WARN"
    INFO = "INFO"


@dataclass
class QualityIssue:
    check: str
    severity: Severity
    count: int
    message: str
    examples: list[Any] = field(default_factory=list)


@dataclass
class QualityResult:
    symbol: str
    interval: str
    source: str
    trust_level: TrustLevel
    quarantined: bool
    rows: int
    issues: list[QualityIssue] = field(default_factory=list)

    def add(self, issue: QualityIssue | None) -> None:
        if issue is not None and issue.count > 0:
            self.issues.append(issue)

    @property
    def errors(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity is Severity.WARN]

    @property
    def passed(self) -> bool:
        """A dataset passes only if it has no ERROR issues. A quarantined
        (LOW-trust) dataset may pass checks but is still not *eligible*."""
        return len(self.errors) == 0

    @property
    def eligible_for_use(self) -> bool:
        """Usable for strategy validation / model training: passed AND HIGH trust."""
        return self.passed and not self.quarantined

    def has(self, check: str) -> bool:
        return any(i.check == check for i in self.issues)


# ── orchestration ───────────────────────────────────────────────────────────────


def run_quality_checks(
    df: pd.DataFrame,
    *,
    interval: str,
    segment: str = "EQ",
    source: str = "unknown",
    trust: TrustLevel | None = None,
    symbol: str | None = None,
    calendar: list | None = None,
    corp_actions: set | None = None,
    symbol_map: set | None = None,
    outlier_threshold: float = DEFAULT_OUTLIER_THRESHOLD,
    ca_threshold: float = DEFAULT_CA_GAP_THRESHOLD,
    now: datetime | None = None,
) -> QualityResult:
    """Run all checks over ``df`` and return a structured result."""
    from backtesting.s3_data_catalog import classify_source_trust

    if trust is None:
        trust = classify_source_trust(source)
    if now is None:
        now = datetime.now(timezone.utc)

    sym = symbol or (str(df["symbol"].iloc[0]) if ("symbol" in df.columns and len(df)) else "?")
    result = QualityResult(
        symbol=sym,
        interval=interval,
        source=source,
        trust_level=trust,
        quarantined=is_quarantined(trust),
        rows=int(len(df)),
    )

    if df.empty:
        result.add(QualityIssue("empty_dataset", Severity.ERROR, 1, "No rows loaded."))
        return result

    result.add(check_timezone(df))
    result.add(check_future_timestamps(df, now=now))
    result.add(check_duplicates(df))
    result.add(check_invalid_ohlc(df))
    result.add(check_nonpositive_prices(df))
    result.add(check_zero_volume(df, segment=segment))
    result.add(check_market_hours(df, interval=interval))
    result.add(check_missing_candles(df, interval=interval))
    result.add(check_missing_trading_days(df, calendar=calendar))
    result.add(check_outlier_jumps(df, threshold=outlier_threshold))
    result.add(check_symbol_mapping(df, symbol_map=symbol_map))
    result.add(check_corporate_action_gaps(df, corp_actions=corp_actions, threshold=ca_threshold))
    return result


# ── individual checks ────────────────────────────────────────────────────────────


def _ist(series: pd.Series) -> pd.Series:
    tz = getattr(series.dt, "tz", None)
    return series.dt.tz_convert(IST) if tz is not None else series


def check_timezone(df: pd.DataFrame) -> QualityIssue | None:
    """Naive timestamps are an ERROR (ambiguous); non-IST offset is a WARN."""
    ts = df["timestamp"]
    n_nat = int(ts.isna().sum())
    tz = getattr(ts.dt, "tz", None)
    if tz is None:
        return QualityIssue(
            "timezone_errors",
            Severity.ERROR,
            int(ts.notna().sum()) + n_nat,
            "Timestamps are timezone-naive (ambiguous). Expected IST (Asia/Kolkata).",
            examples=[str(ts.dropna().iloc[0])] if ts.notna().any() else [],
        )
    if n_nat:
        return QualityIssue(
            "timezone_errors", Severity.ERROR, n_nat, f"{n_nat} unparseable timestamp(s)."
        )
    return None


def check_future_timestamps(df: pd.DataFrame, *, now: datetime) -> QualityIssue | None:
    ts = df["timestamp"]
    if getattr(ts.dt, "tz", None) is None:
        cmp = pd.Timestamp(now).tz_localize(None)
    else:
        cmp = pd.Timestamp(now)
        if cmp.tz is None:
            cmp = cmp.tz_localize("UTC")
    future = df[ts > cmp]
    if len(future):
        return QualityIssue(
            "future_timestamps",
            Severity.ERROR,
            int(len(future)),
            f"{len(future)} timestamp(s) in the future (> {cmp.isoformat()}).",
            examples=[str(x) for x in future["timestamp"].head(3).tolist()],
        )
    return None


def check_duplicates(df: pd.DataFrame) -> QualityIssue | None:
    subset = [c for c in ("symbol", "interval", "timestamp") if c in df.columns]
    dup_mask = df.duplicated(subset=subset, keep=False)
    n = int(dup_mask.sum())
    if n:
        ex = df.loc[dup_mask, "timestamp"].astype(str).head(3).tolist()
        return QualityIssue(
            "duplicate_timestamps", Severity.ERROR, n, f"{n} duplicate candle row(s).", ex
        )
    return None


def check_invalid_ohlc(df: pd.DataFrame) -> QualityIssue | None:
    o, h, low, c = df["open"], df["high"], df["low"], df["close"]
    bad = ~((low <= o) & (o <= h) & (low <= c) & (c <= h) & (h >= low))
    bad = bad & o.notna() & h.notna() & low.notna() & c.notna()
    n = int(bad.sum())
    if n:
        ex = [
            f"{r.timestamp}: O={r.open} H={r.high} L={r.low} C={r.close}"
            for r in df[bad].head(3).itertuples()
        ]
        return QualityIssue(
            "invalid_ohlc", Severity.ERROR, n, f"{n} row(s) violate low<=open,close<=high.", ex
        )
    return None


def check_nonpositive_prices(df: pd.DataFrame) -> QualityIssue | None:
    cols = ["open", "high", "low", "close"]
    bad = (df[cols] <= 0).any(axis=1)
    n = int(bad.sum())
    if n:
        ex = [str(x) for x in df.loc[bad, "timestamp"].head(3).tolist()]
        return QualityIssue(
            "nonpositive_prices", Severity.ERROR, n, f"{n} row(s) with zero/negative price.", ex
        )
    return None


def check_zero_volume(df: pd.DataFrame, *, segment: str) -> QualityIssue | None:
    # Indices legitimately have zero traded volume — only flag tradable segments.
    if segment.upper() == "INDEX" or "volume" not in df.columns:
        return None
    bad = df["volume"].fillna(0) <= 0
    n = int(bad.sum())
    if n:
        ex = [str(x) for x in df.loc[bad, "timestamp"].head(3).tolist()]
        return QualityIssue(
            "zero_volume", Severity.WARN, n, f"{n} row(s) with zero volume (EQ).", ex
        )
    return None


def check_market_hours(df: pd.DataFrame, *, interval: str) -> QualityIssue | None:
    if interval == "1d":
        return None
    ist = _ist(df["timestamp"])
    tod = ist.dt.time
    bad_mask = (tod < MARKET_OPEN) | (tod > MARKET_CLOSE)
    bad_mask = bad_mask & ist.notna()
    n = int(bad_mask.sum())
    if n:
        ex = [str(x) for x in ist[bad_mask].head(3).tolist()]
        return QualityIssue(
            "market_hours_violation",
            Severity.ERROR,
            n,
            f"{n} candle(s) outside NSE hours {MARKET_OPEN}-{MARKET_CLOSE} IST.",
            ex,
        )
    return None


def check_missing_candles(df: pd.DataFrame, *, interval: str) -> QualityIssue | None:
    if interval == "1d" or interval not in BARS_PER_DAY:
        return None
    expected = BARS_PER_DAY[interval]
    ist = _ist(df["timestamp"])
    by_day = df.assign(_d=ist.dt.date).groupby("_d").size()
    short = by_day[by_day < expected]
    missing = int((expected - short).clip(lower=0).sum())
    if missing:
        return QualityIssue(
            "missing_candles",
            Severity.WARN,
            missing,
            f"{missing} candle(s) missing vs {expected}/day across {len(short)} day(s).",
            [f"{d}: {int(short[d])}/{expected}" for d in list(short.index)[:3]],
        )
    return None


def check_missing_trading_days(df: pd.DataFrame, *, calendar: list | None) -> QualityIssue | None:
    ist = _ist(df["timestamp"])
    present = sorted({d for d in ist.dt.date.tolist() if pd.notna(d)})
    if len(present) < 2:
        return None
    if calendar:
        expected = [d for d in calendar if present[0] <= d <= present[-1]]
        msg_suffix = "vs supplied trading calendar"
    else:
        expected = [d.date() for d in pd.bdate_range(present[0], present[-1])]
        msg_suffix = "vs business-day calendar (NSE holidays not excluded)"
    missing = sorted(set(expected) - set(present))
    if missing:
        return QualityIssue(
            "missing_trading_days",
            Severity.WARN,
            len(missing),
            f"{len(missing)} trading day(s) absent {msg_suffix}.",
            [str(d) for d in missing[:5]],
        )
    return None


def check_outlier_jumps(
    df: pd.DataFrame, *, threshold: float = DEFAULT_OUTLIER_THRESHOLD
) -> QualityIssue | None:
    total = 0
    examples: list[str] = []
    for sym, g in df.sort_values("timestamp").groupby("symbol"):
        pct = g["close"].pct_change().abs()
        hits = g[pct > threshold]
        total += int(len(hits))
        for r in hits.head(2).itertuples():
            examples.append(f"{sym}@{r.timestamp}: close={r.close}")
    if total:
        return QualityIssue(
            "outlier_jumps",
            Severity.WARN,
            total,
            f"{total} bar-to-bar move(s) exceeding {threshold:.0%} (possible bad tick / unadjusted split).",
            examples[:5],
        )
    return None


def check_symbol_mapping(df: pd.DataFrame, *, symbol_map: set | None) -> QualityIssue | None:
    issues = 0
    examples: list[str] = []
    if "isin" in df.columns:
        miss_isin = df["isin"].isna() | (df["isin"].astype("string").fillna("") == "")
        issues += int(miss_isin.sum())
        examples += [str(s) for s in df.loc[miss_isin, "symbol"].unique()[:3]]
    if symbol_map is not None and "symbol" in df.columns:
        unmapped = df[~df["symbol"].isin(symbol_map)]
        issues += int(len(unmapped))
        examples += [str(s) for s in unmapped["symbol"].unique()[:3]]
    if issues:
        return QualityIssue(
            "symbol_mapping_gaps",
            Severity.WARN,
            issues,
            "Rows lacking an ISIN mapping (symbols change over 15y — ISIN is the stable key).",
            examples[:5],
        )
    return None


def check_corporate_action_gaps(
    df: pd.DataFrame, *, corp_actions: set | None = None, threshold: float = DEFAULT_CA_GAP_THRESHOLD
) -> QualityIssue | None:
    ist = _ist(df["timestamp"])
    g = df.assign(_d=ist.dt.date).sort_values("timestamp")
    daily = g.groupby("_d").agg(first_open=("open", "first"), last_close=("close", "last"))
    if len(daily) < 2:
        return None
    prev_close = daily["last_close"].shift(1)
    ratio = (daily["first_open"] / prev_close).abs()
    hit_mask = (ratio > 1 + threshold) | (ratio < 1 - threshold)
    hit_mask = hit_mask & prev_close.notna()
    if corp_actions:
        hit_mask = hit_mask & ~daily.index.isin(corp_actions)
    hits = daily[hit_mask]
    if len(hits):
        return QualityIssue(
            "corporate_action_gaps",
            Severity.WARN,
            int(len(hits)),
            f"{len(hits)} overnight gap(s) >{threshold:.0%} with no corporate-action record (suspected missing CA / unadjusted split).",
            [str(d) for d in list(hits.index)[:3]],
        )
    return None


# ── markdown report ─────────────────────────────────────────────────────────────


def to_markdown(results: list[QualityResult], *, title: str, notes: str = "") -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
                 "backtesting lab data-quality validator · backtest-only._")
    lines.append("")
    if notes:
        lines.append(notes)
        lines.append("")

    # Summary table.
    lines.append("## Summary")
    lines.append("")
    lines.append("| Source | Trust | Zone | Symbol | Interval | Rows | Errors | Warnings | Passed | Eligible |")
    lines.append("|---|---|---|---|---|---:|---:|---:|---|---|")
    for r in results:
        zone = "quarantine" if r.quarantined else "lake"
        lines.append(
            f"| {r.source} | {r.trust_level.value} | {zone} | {r.symbol} | {r.interval} | "
            f"{r.rows} | {len(r.errors)} | {len(r.warnings)} | "
            f"{'✅' if r.passed else '❌'} | {'✅' if r.eligible_for_use else '❌'} |"
        )
    lines.append("")

    # Per-dataset detail.
    for r in results:
        lines.append(f"## {r.source} — {r.symbol} [{r.interval}]")
        lines.append("")
        lines.append(
            f"- Trust: **{r.trust_level.value}** · "
            f"Zone: **{'quarantine' if r.quarantined else 'lake'}** · Rows: **{r.rows}**"
        )
        if r.quarantined:
            lines.append(
                "- ⚠️ **Quarantined (LOW trust).** Not eligible for strategy validation or "
                "model training until quality + license review and explicit approval."
            )
        lines.append(f"- Result: **{'PASS' if r.passed else 'FAIL'}** "
                     f"({len(r.errors)} error(s), {len(r.warnings)} warning(s))")
        lines.append("")
        if not r.issues:
            lines.append("No issues detected.")
            lines.append("")
            continue
        lines.append("| Check | Severity | Count | Message | Examples |")
        lines.append("|---|---|---:|---|---|")
        for i in sorted(r.issues, key=lambda x: (x.severity is not Severity.ERROR, x.check)):
            ex = "; ".join(str(e) for e in i.examples[:3])
            ex = ex.replace("|", "\\|")
            msg = i.message.replace("|", "\\|")
            lines.append(f"| {i.check} | {i.severity.value} | {i.count} | {msg} | {ex} |")
        lines.append("")

    return "\n".join(lines)
