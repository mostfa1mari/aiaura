"""AI AURA — continuous OTC data collector (read-only).

Runs headless, subscribes to one or more OTC assets, and persists every tick
to data/raw/ticks/ (Parquet). Prints a periodic status line with per-asset
tick counts, data-quality grade, and connection health. Reconnection /
zombie-recovery is handled by the provider's supervisor. Stop with Ctrl+C
(flushes cleanly).

Why this matters: features, backtests, and any ML edge can only be validated
on a substantial history of real OTC ticks. This collector is how that history
accumulates. Leave it running (a valid PO_SSID in .env is required; when the
session expires the provider stops and the status line says so — recapture and
restart).

Usage:
    .venv/Scripts/python scripts/collect.py --assets EURUSD_otc,GBPUSD_otc
    .venv/Scripts/python scripts/collect.py --all-otc        # every available OTC pair
    .venv/Scripts/python scripts/collect.py --status-every 30
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.market_data.pocket_option_provider import PocketOptionMarketDataProvider
from services.market_data.quality import TickQualityMonitor, summarize_connection_events
from services.market_data.security import load_ssid, setup_logging
from services.market_data.storage import TickStore

logger = logging.getLogger("collect")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI AURA continuous OTC collector")
    parser.add_argument("--assets", default="EURUSD_otc",
                        help="comma-separated OTC symbols")
    parser.add_argument("--all-otc", action="store_true",
                        help="collect every currently-available OTC asset")
    parser.add_argument("--period", type=int, default=1, help="subscribe period (s)")
    parser.add_argument("--status-every", type=float, default=30.0)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after N seconds (0 = run until Ctrl+C)")
    parser.add_argument("--max-assets", type=int, default=20,
                        help="safety cap when --all-otc is used")
    args = parser.parse_args()

    ssid = load_ssid()
    setup_logging(ssid, level=logging.INFO,
                  log_file=PROJECT_ROOT / "logs" / "collect.log", console=False)

    provider = PocketOptionMarketDataProvider(ssid)
    store = TickStore(PROJECT_ROOT / "data" / "raw" / "ticks")
    quality = TickQualityMonitor()
    provider.add_tick_listener(store.append)
    provider.add_tick_listener(quality.observe)

    stop = {"flag": False}

    def _handle_sigint(_sig, _frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle_sigint)

    print("AI AURA — OTC COLLECTOR\nConnecting...")
    provider.connect()

    if args.all_otc:
        otc = provider.get_otc_assets()
        assets = sorted(otc)[: args.max_assets]
        if len(otc) > args.max_assets:
            print(f"note: {len(otc)} OTC assets available; collecting first "
                  f"{args.max_assets} (raise with --max-assets).")
    else:
        assets = [a.strip() for a in args.assets.split(",") if a.strip()]

    subscribed = []
    for asset in assets:
        try:
            provider.subscribe(asset, args.period)
            subscribed.append(asset)
        except Exception as exc:
            print(f"skip {asset}: {exc}")
    if not subscribed:
        print("no assets subscribed; exiting.")
        provider.disconnect()
        return 1

    print(f"collecting {len(subscribed)} asset(s): {', '.join(subscribed)}")
    print("Ctrl+C to stop.\n")

    started = time.time()
    try:
        while not stop["flag"]:
            time.sleep(args.status_every)
            if args.duration and (time.time() - started) >= args.duration:
                break
            health = provider.health_check()
            conn = summarize_connection_events(provider.connection_events)
            elapsed = int(time.time() - started)
            parts = []
            for asset in subscribed:
                rep = quality.report_for(asset)
                n = rep.tick_count if rep else 0
                grade = rep.quality_grade if rep else "—"
                parts.append(f"{asset}={n}({grade})")
            print(
                f"[{elapsed:>6}s] status={health.status} "
                f"persisted={store.ticks_persisted} "
                f"reconnects={conn['reconnects']} "
                f"| " + "  ".join(parts)
                + (f"  DETAIL: {health.detail}" if health.detail else "")
            )
            if health.status == "DOWN" and provider._terminal_reason:
                print("provider stopped (terminal):", provider._terminal_reason)
                break
    finally:
        print("\nflushing and disconnecting...")
        provider.remove_tick_listener(store.append)
        provider.remove_tick_listener(quality.observe)
        store.close()
        provider.disconnect()
        print(f"done. persisted {store.ticks_persisted} ticks in "
              f"{len(store.files_written)} parquet file(s) under data/raw/ticks/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
