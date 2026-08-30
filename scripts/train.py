"""Run a self-learning training cycle on real Pocket Option candle history.

Builds a no-look-ahead dataset from historical candles, trains + walk-forward-
evaluates challenger models, and promotes a champion ONLY if it shows positive,
significant out-of-sample expectancy. Honest by construction: on data with no
real edge, nothing is deployed.

    .venv/Scripts/python scripts/train.py --asset EURUSD_otc --expiry 60 --pages 8

Needs a valid PO_SSID in .env. Stop the API server first (one live session per
account). More history (higher --pages) → more reliable training.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.learning_engine.registry import ModelRegistry
from services.learning_engine.self_learning import MIN_ROWS, run_training_cycle
from services.market_data.pocket_option_provider import PocketOptionMarketDataProvider
from services.market_data.security import load_ssid, setup_logging

# expiry -> (analysis timeframe seconds, horizon seconds)
EXPIRY_TIMEFRAME = {5: 5, 15: 5, 30: 10, 60: 15, 180: 30, 300: 60, 900: 60}


def main() -> int:
    parser = argparse.ArgumentParser(description="AI AURA training cycle")
    parser.add_argument("--asset", default="EURUSD_otc")
    parser.add_argument("--expiry", type=int, default=60, choices=sorted(EXPIRY_TIMEFRAME))
    parser.add_argument("--pages", type=int, default=8, help="history pages (more = more data)")
    parser.add_argument("--payout", type=float, default=0.8)
    args = parser.parse_args()

    timeframe = EXPIRY_TIMEFRAME[args.expiry]
    horizon = float(args.expiry)

    ssid = load_ssid()
    setup_logging(ssid, level=logging.WARNING,
                  log_file=PROJECT_ROOT / "logs" / "train.log", console=True)

    provider = PocketOptionMarketDataProvider(ssid)
    print(f"connecting… (asset={args.asset}, timeframe={timeframe}s, horizon={horizon}s)")
    provider.connect()
    provider.subscribe(args.asset)
    provider.wait_for_first_tick(args.asset, timeout_s=10)

    print(f"fetching ~{args.pages} pages of {timeframe}s candles…")
    candles = provider.get_historical_candles(args.asset, timeframe, pages=args.pages)
    provider.disconnect()
    print(f"got {len(candles)} candles")
    if len(candles) < MIN_ROWS + 60:
        print(f"WARNING: likely too few candles for reliable training "
              f"(need ~{MIN_ROWS}+ dataset rows). Increase --pages or collect more data.")

    registry = ModelRegistry(PROJECT_ROOT / "models")
    result = run_training_cycle(candles, horizon_s=horizon, registry=registry, payout=args.payout)
    print("\n=== training cycle ===")
    print(json.dumps(result, indent=2, default=str))
    if result.get("status") == "trained":
        if result["promoted"]:
            print(f"\n✅ challenger PROMOTED to champion ({result['version']}). The app will use it.")
        else:
            print("\nℹ️ challenger kept but NOT deployed — no significant edge over "
                  "break-even / the current champion. This is the honest outcome "
                  "when the data shows no edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
