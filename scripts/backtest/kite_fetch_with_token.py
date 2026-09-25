#!/usr/bin/env python3
"""
QuantEmbrace — one-shot Kite index/VIX fetch from a fresh request_token (NO DynamoDB).

WHY THIS EXISTS
---------------
`scripts/zerodha_login.py` is built for the *trading stack*: after exchanging the
request_token it persists the access_token to the DynamoDB `sessions` table. For a
plain **backtesting data pull** that DynamoDB write is both unnecessary and a hard
dependency on LocalStack (localhost:4566) — if LocalStack is down the whole login
fails *after* the Kite exchange already succeeded, burning the single-use
request_token. This tool does the minimum: exchange → fetch into the Parquet lake,
entirely in-process. The access token is never persisted, never printed.

USAGE (run LOCALLY; the request_token is single-use and lives ~5 min)
--------------------------------------------------------------------
  1. Open the login URL in a browser and sign in:
        https://kite.zerodha.com/connect/login?api_key=<your_api_key>&v=3
  2. Kite redirects to your callback with ?request_token=XXXX — copy XXXX.
  3. IMMEDIATELY run:
        python scripts/backtest/kite_fetch_with_token.py --request-token XXXX

By default it fetches NIFTY50 + INDIA VIX daily (2020→2025) into the lake and then
runs the volatility-premium screen. Backtest-only: historical data, no orders, no
live/paper trading, no capital changes.

Needs ZERODHA_API_KEY + ZERODHA_API_SECRET in .env (already present).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def main() -> int:
    ap = argparse.ArgumentParser(description="One-shot Kite index/VIX fetch from a request_token (no DynamoDB)")
    ap.add_argument("--request-token", required=True, help="Fresh single-use request_token from the Kite login redirect")
    ap.add_argument("--indices", default="niftyvix", help="Index bundle (default: niftyvix)")
    ap.add_argument("--intervals", default="1d", help="Intervals (default: 1d)")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--base", default=str(_REPO / "backtest-data"))
    ap.add_argument("--no-screen", action="store_true", help="Fetch only; skip running the VRP screen")
    args = ap.parse_args()

    import os
    api_key = os.environ.get("ZERODHA_API_KEY")
    secret = os.environ.get("ZERODHA_API_SECRET")
    if not api_key or not secret:
        print("ERROR: ZERODHA_API_KEY / ZERODHA_API_SECRET not found in env/.env", file=sys.stderr)
        return 2

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        print("ERROR: kiteconnect not installed (pip install kiteconnect)", file=sys.stderr)
        return 2

    import fetch_zerodha_indices as F

    print("=" * 72)
    print("QuantEmbrace — Kite index/VIX one-shot fetch (no DynamoDB). Backtest-only.")
    print("=" * 72)
    kite = KiteConnect(api_key=api_key)
    try:
        sess = kite.generate_session(args.request_token, api_secret=secret)
    except Exception as exc:
        print(f"\n[ERROR] Token exchange failed: {exc}")
        print("  request_tokens are single-use and expire in ~5 min. Re-open the login URL,")
        print("  grab a FRESH request_token, and run this again immediately.")
        return 3
    kite.set_access_token(sess["access_token"])
    try:
        kite.reqsession.timeout = (5, 15)
    except Exception:
        pass
    print(f"  token exchange OK (user {sess.get('user_id', '?')}); access token resolved (not printed, not persisted).")

    symbols = F.INDEX_BUNDLES.get(args.indices, F.INDEX_BUNDLES["niftyvix"])
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    rc = F.fetch(kite, symbols, intervals,
                 date.fromisoformat(args.start), date.fromisoformat(args.end),
                 Path(args.base), force=True, verbose=False)
    if rc != 0:
        return rc

    if args.no_screen:
        print("\n  Fetch done. Run the screen with:  python scripts/backtest/run_vol_premium_study.py")
        return 0

    print("\n  Running the volatility-premium screen on the freshly-fetched HIGH-trust lake data...\n")
    screen = _REPO / "scripts" / "backtest" / "run_vol_premium_study.py"
    return subprocess.run([sys.executable, str(screen), "--base", args.base]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
