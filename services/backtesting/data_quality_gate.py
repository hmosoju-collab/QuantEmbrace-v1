"""Pre-run data-quality gate for the QuantEmbrace backtesting lab.

Samples a representative window of lake data for every symbol in a ``RunSpec``
and runs the full ``data_quality.run_quality_checks()`` suite before the replay
engine touches a single bar. A single ERROR-level finding on any symbol — or a
LOW-trust (quarantined) dataset — blocks the run entirely.

Design:
    * Sampling: first 90 trading days of the requested date range per symbol ×
      timeframe.  Keeps gate latency proportional to symbol count, not run length.
      For runs ≤ 90 days the full window is loaded.
    * Blocking policy: any blocked symbol → run refuses to start; WARN-only
      symbols are logged but do not block.
    * CW emission: ``DQGateFailed`` metric is emitted on any failure so the
      CloudWatch alarm in monitoring_backtest can alert immediately.
    * Report: a markdown quality report is written locally and (optionally)
      uploaded to S3 under ``{results_bucket}/data-quality/{run_id}/report.md``.

Backtest-only: reads from ``quantembrace-backtest-data``; writes QA reports to
``quantembrace-backtest-results``. No broker APIs, no live tables.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from backtesting.data_loader import load_candles
from backtesting.data_quality import QualityResult, run_quality_checks, to_markdown
from backtesting.run_registry import RunSpec
from backtesting.s3_data_catalog import DataCatalog, TrustLevel

logger = logging.getLogger(__name__)

# Days to sample from the start of the requested date range.
_SAMPLE_DAYS = 90


@dataclass
class GateResult:
    """Structured outcome of a DataQualityGate.check() call."""

    run_id: str
    passed: bool  # True only when every symbol passes + is HIGH-trust
    symbol_results: dict[str, QualityResult] = field(default_factory=dict)
    blocked_symbols: list[str] = field(default_factory=list)  # ERROR or quarantined
    warn_only_symbols: list[str] = field(default_factory=list)  # WARN but not blocking
    report_s3_path: str | None = None
    markdown_report: str = ""

    def summary_line(self) -> str:
        n = len(self.symbol_results)
        ok = n - len(self.blocked_symbols)
        return (
            f"DQ gate: {'PASS' if self.passed else 'FAIL'} — "
            f"{ok}/{n} symbols clean, "
            f"{len(self.blocked_symbols)} blocked, "
            f"{len(self.warn_only_symbols)} warn-only"
        )


class DataQualityGate:
    """Validates lake data for a ``RunSpec`` before the replay engine runs.

    Args:
        catalog: ``DataCatalog`` for building lake S3 paths.
        cw_emitter: Optional CW emitter; emits ``DQGateFailed`` on any failure.
        s3_client: Optional injected S3 client (tests / LocalStack). None → uses
            the shared sanctioned factory on first access.
        results_bucket: S3 bucket for QA reports (``quantembrace-backtest-results``).
        segment: NSE segment to validate (default ``EQ``).
        market: Market label (default ``NSE``).
    """

    def __init__(
        self,
        *,
        catalog: DataCatalog | None = None,
        cw_emitter: Any = None,
        s3_client: Any = None,
        results_bucket: str | None = None,
    ) -> None:
        self._catalog = catalog or DataCatalog()
        self._cw = cw_emitter
        self._s3_client = s3_client
        self._results_bucket = results_bucket

    def check(
        self,
        spec: RunSpec,
        *,
        timeframes: list[str] | None = None,
        segment: str = "EQ",
        market: str = "NSE",
    ) -> GateResult:
        """Load a sample window per symbol and run all quality checks.

        Args:
            spec: Run specification from which symbols and date range are taken.
            timeframes: Intervals to check (default: ``["1d"]``).
            segment: NSE segment label for path building and OHLC checks.
            market: Market label.

        Returns:
            ``GateResult`` — callers should check ``.passed`` before proceeding.
        """
        run_id = spec.run_id()
        timeframes = timeframes or ["1d"]
        start_date = _as_date(spec.start_date)
        end_date = _as_date(spec.end_date)
        sample_end = min(start_date + timedelta(days=_SAMPLE_DAYS), end_date)

        symbol_results: dict[str, QualityResult] = {}

        for symbol in spec.symbols:
            for interval in timeframes:
                key = f"{symbol}|{interval}"
                qr = self._check_symbol(
                    symbol=symbol,
                    interval=interval,
                    start_date=start_date,
                    end_date=sample_end,
                    segment=segment,
                    market=market,
                )
                symbol_results[key] = qr
                if qr.eligible_for_use:
                    logger.debug("[dq-gate] %s ELIGIBLE (%d rows)", key, qr.rows)
                elif qr.passed and qr.quarantined:
                    logger.warning("[dq-gate] %s QUARANTINED (LOW trust) — blocking", key)
                else:
                    logger.warning(
                        "[dq-gate] %s FAILED — %d error(s): %s",
                        key, len(qr.errors), [i.check for i in qr.errors],
                    )

        blocked = [k for k, r in symbol_results.items() if not r.eligible_for_use]
        warn_only = [
            k for k, r in symbol_results.items()
            if r.passed and not r.quarantined and r.warnings
        ]
        passed = len(blocked) == 0

        # Build markdown report.
        all_results = list(symbol_results.values())
        title = f"Data Quality Gate — {run_id} — {spec.strategy}"
        notes = (
            f"Symbols: {list(spec.symbols)} · Date window: {start_date} → {sample_end} "
            f"(sample of full range {start_date} → {end_date})"
        )
        md = to_markdown(all_results, title=title, notes=notes)

        # Write report to S3 (best-effort; failure is non-blocking for the gate itself).
        report_s3_path: str | None = None
        if self._results_bucket:
            report_s3_path = self._write_report(run_id, md)

        result = GateResult(
            run_id=run_id,
            passed=passed,
            symbol_results=symbol_results,
            blocked_symbols=blocked,
            warn_only_symbols=warn_only,
            report_s3_path=report_s3_path,
            markdown_report=md,
        )

        if not passed and self._cw is not None:
            try:
                self._cw.dq_gate_failed()
            except Exception:
                pass

        level = logging.INFO if passed else logging.ERROR
        logger.log(level, "[dq-gate] %s", result.summary_line())
        return result

    # ── internals ────────────────────────────────────────────────────────────────

    def _check_symbol(
        self,
        *,
        symbol: str,
        interval: str,
        start_date: date,
        end_date: date,
        segment: str,
        market: str,
    ) -> QualityResult:
        """Load a sample and run quality checks for one (symbol, interval) pair."""
        # Build the lake path for the first year of the sample window.
        path = self._catalog.lake_partition(
            symbol=symbol,
            interval=interval,
            year=start_date.year,
            segment=segment,
            market=market,
        )
        try:
            result = load_candles(
                path,
                symbol=symbol,
                interval=interval,
                segment=segment,
                market=market,
                date_from=start_date,
                date_to=end_date,
                source_name="bhavcopy",
                s3_client=self._s3_client,
            )
            if result.df.empty:
                # Try without date filter — maybe the year partition key is slightly
                # different. Return an ERROR result if still empty.
                result = load_candles(
                    path,
                    symbol=symbol,
                    interval=interval,
                    segment=segment,
                    market=market,
                    source_name="bhavcopy",
                    s3_client=self._s3_client,
                )
        except Exception as exc:
            logger.warning("[dq-gate] Could not load %s|%s: %s", symbol, interval, exc)
            # Fabricate an ERROR QualityResult so the gate blocks this symbol.
            from backtesting.data_quality import QualityIssue, Severity
            qr = QualityResult(
                symbol=symbol,
                interval=interval,
                source="bhavcopy",
                trust_level=TrustLevel.HIGH,
                quarantined=False,
                rows=0,
            )
            qr.add(QualityIssue(
                check="load_error",
                severity=Severity.ERROR,
                count=1,
                message=f"Could not load data from lake: {exc}",
            ))
            return qr

        return run_quality_checks(
            result.df,
            interval=interval,
            segment=segment,
            source=result.source,
            trust=result.trust_level,
            symbol=symbol,
        )

    def _write_report(self, run_id: str, md: str) -> str | None:
        """Upload the markdown report to S3. Non-fatal on failure."""
        key = f"data-quality/{run_id}/report.md"
        client = self._s3_client
        if client is None:
            try:
                from shared.aws.clients import get_s3_client
                client = get_s3_client()
            except Exception:
                return None
        try:
            client.put_object(
                Bucket=self._results_bucket,
                Key=key,
                Body=md.encode(),
                ContentType="text/markdown",
            )
            s3_path = f"s3://{self._results_bucket}/{key}"
            logger.info("[dq-gate] Report written: %s", s3_path)
            return s3_path
        except Exception as exc:
            logger.warning("[dq-gate] Report upload non-fatal: %s", exc)
            return None


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
