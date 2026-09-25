# QuantEmbrace — XSMOM survivorship falsification test (ADR-041 Phase 2b)
#
# PURPOSE: The local screen's XSMOM (12-1 momentum, top-10 of today's mega-caps)
# passed with Sharpe 0.86 — but its universe is survivorship-SELECTED (stocks
# chosen because they won). This algorithm re-runs the same strategy on
# QuantConnect's survivorship-bias-free point-in-time universe (top-100 by
# dollar volume, selected monthly AS OF each date) with QC's default fee model.
#
# PRE-REGISTERED VERDICT RULE (declared before running):
#   Net Sharpe >= 0.80 on this survivorship-free run  → XSMOM stays shortlisted.
#   Net Sharpe <  0.80                                → XSMOM is falsified; dies.
#
# HOW TO RUN (operator, ~10 minutes, $0):
#   1. Create a free account at quantconnect.com.
#   2. Web IDE → new Python algorithm project → replace main.py with this file.
#   3. Click Backtest (free tier includes cloud backtests on QC's data).
#   4. Report back: Sharpe Ratio, CAGR, Max Drawdown, and the annual returns
#      table from the backtest report.
#
# Advisory only. No live trading. Recommends; a human promotes.

from AlgorithmImports import *


class XsmomSurvivorshipTest(QCAlgorithm):
    LOOKBACK = 252          # 12 months
    SKIP = 21               # skip most recent month (12-1 momentum)
    TOP_UNIVERSE = 100      # point-in-time top-100 by dollar volume
    HOLD = 10               # top-10 equal weight

    def Initialize(self):
        self.SetStartDate(2006, 6, 30)
        self.SetEndDate(2026, 7, 9)
        self.SetCash(1_000_000)
        self.UniverseSettings.Resolution = Resolution.Daily
        self.AddUniverse(self.CoarseFilter)
        self.AddEquity("SPY", Resolution.Daily)  # schedule anchor + benchmark
        self.SetBenchmark("SPY")
        self.Schedule.On(self.DateRules.MonthStart("SPY"),
                         self.TimeRules.AfterMarketOpen("SPY", 30),
                         self.Rebalance)
        self.rebalance_due = False

    def CoarseFilter(self, coarse):
        # Point-in-time large/mega-cap proxy: liquid, real companies, no penny
        # names. QC's universe data is survivorship-bias-free.
        selected = [c for c in coarse
                    if c.HasFundamentalData and c.Price > 5]
        selected.sort(key=lambda c: c.DollarVolume, reverse=True)
        return [c.Symbol for c in selected[:self.TOP_UNIVERSE]]

    def Rebalance(self):
        symbols = [s for s in self.ActiveSecurities.Keys
                   if s.Value != "SPY" and s.SecurityType == SecurityType.Equity]
        if not symbols:
            return
        hist = self.History(symbols, self.LOOKBACK + 5, Resolution.Daily)
        if hist.empty:
            return
        scores = {}
        closes = hist["close"].unstack(level=0)
        for s in symbols:
            key = str(s.ID) if str(s.ID) in closes.columns else s
            try:
                series = closes[key].dropna()
            except KeyError:
                continue
            if len(series) < self.LOOKBACK:
                continue
            p_then = series.iloc[-self.LOOKBACK]
            p_skip = series.iloc[-self.SKIP]
            if p_then > 0:
                scores[s] = p_skip / p_then - 1
        if len(scores) < 30:
            return
        top = sorted(scores, key=scores.get, reverse=True)[:self.HOLD]
        # liquidate anything no longer held
        for holding in [h for h in self.Portfolio.Values if h.Invested]:
            if holding.Symbol not in top:
                self.Liquidate(holding.Symbol)
        for s in top:
            self.SetHoldings(s, 1.0 / self.HOLD)
