# QuantEmbrace — RPLITE cross-check algorithm (ADR-041 Phase 2b)
#
# Runs the shortlisted RPLITE strategy (SPY/TLT/GLD, inverse-63d-vol, monthly)
# inside the LEAN engine on the SAME total-return series as the pandas screen
# (exported by export_lake_to_lean.py). Zero costs — this is an ENGINE parity
# check; the cost model is validated separately in the pandas harness.
#
# Convention parity: the pandas screen decides weights at month-end close T and
# fills at the close of T+1. Here, the first bar of the new month (T+1) fires
# OnData; weights are computed from returns up to T; market orders fill at the
# latest custom-data value (T+1's close). Same boundary.

from collections import deque

from AlgorithmImports import *


class UsEodTr(PythonData):
    def GetSource(self, config, date, isLiveMode):
        path = os.path.join(Globals.DataFolder, "us_eod_tr",
                            config.Symbol.Value.lower() + ".csv")
        return SubscriptionDataSource(path, SubscriptionTransportMedium.LocalFile)

    def Reader(self, config, line, date, isLiveMode):
        if not line or not line[0].isdigit():
            return None
        parts = line.split(",")
        bar = UsEodTr()
        bar.Symbol = config.Symbol
        bar.Time = datetime.strptime(parts[0], "%Y%m%d")
        bar.EndTime = bar.Time + timedelta(hours=17)  # after the 16:00 close
        bar.Value = float(parts[4])
        bar["Volume"] = float(parts[5])
        return bar


class RpliteCrossCheck(QCAlgorithm):
    VOL_LOOKBACK = 63

    def Initialize(self):
        # Starts 2006-03-01 so the 63-day vol window is warm by end-June and
        # the first aligned rebalance matches the pandas screen (fill
        # 2006-07-03). Parity is compared from 2006-08-01 to skip the seam.
        self.SetStartDate(2006, 3, 1)
        self.SetEndDate(2026, 7, 9)
        self.SetCash(1_000_000)
        self.SetTimeZone("America/New_York")
        # Engine-parity run: the pandas leg has no margin/settlement model at
        # all (fully-invested, unlevered, cash always available). The prior
        # per-security BuyingPowerModel.Null override still left the
        # account on Margin type, which kept enforcing an initial-margin
        # check underneath it — 149 "Insufficient buying power" rejections
        # over the 20y run (incl. 2022-09-01, 2022-10-03, 2023-11-01),
        # silently leaving stale weights in exactly the highest-vol months
        # and driving the LEAN-vs-pandas parity RMSE. Cash account type has
        # no margin concept, so combined with immediate settlement below,
        # rebalance orders can no longer be rejected for buying power.
        self.SetBrokerageModel(BrokerageName.DEFAULT, AccountType.Cash)
        self.assets = ["spy", "tlt", "gld"]
        self.symbols = {}
        for a in self.assets:
            sec = self.AddData(UsEodTr, a, Resolution.Daily)
            sec.SetFeeModel(ConstantFeeModel(0))
            sec.SetBuyingPowerModel(BuyingPowerModel.Null)
            sec.SetSettlementModel(ImmediateSettlementModel())
            self.symbols[a] = sec.Symbol
        # maxlen 64: at the T+1 bar the deque holds 64 returns ending T+1;
        # dropping the last yields exactly the 63 returns ending T that the
        # pandas screen uses.
        self.rets = {a: deque(maxlen=self.VOL_LOOKBACK + 1) for a in self.assets}
        self.last_px = {}
        self.last_rebalance_month = None

    def OnData(self, data):
        # accumulate daily returns per asset
        for a in self.assets:
            s = self.symbols[a]
            if data.ContainsKey(s) and data[s] is not None:
                px = float(data[s].Value)
                if a in self.last_px and self.last_px[a] > 0:
                    self.rets[a].append(px / self.last_px[a] - 1)
                self.last_px[a] = px

        month = (self.Time.year, self.Time.month)
        if month == self.last_rebalance_month:
            return
        if not all(len(self.rets[a]) == self.VOL_LOOKBACK + 1 for a in self.assets):
            return
        if not all(a in self.last_px for a in self.assets):
            return

        # first bar of a new month (T+1): weights from the 63 returns up to T
        # (today's return was just appended — drop it from the window copy)
        import statistics
        vols = {}
        for a in self.assets:
            window = list(self.rets[a])[:-1]
            vols[a] = statistics.pstdev(window)
        if any(v == 0 for v in vols.values()):
            return
        inv = {a: 1.0 / v for a, v in vols.items()}
        total = sum(inv.values())
        for a in self.assets:
            self.SetHoldings(self.symbols[a], inv[a] / total)
        self.last_rebalance_month = month
