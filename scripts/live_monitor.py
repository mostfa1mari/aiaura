"""AI AURA — LIVE OTC console monitor (read-only).

Usage:
    .venv/Scripts/python scripts/live_monitor.py [--asset EURUSD_otc] [--no-store]

Shows live connection/tick state for one OTC asset and (by default) persists
every tick to data/raw/ticks/. Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.market_data.pocket_option_provider import PocketOptionMarketDataProvider
from services.market_data.security import load_ssid, setup_logging
from services.market_data.storage import TickStore


def main() -> int:
    parser = argparse.ArgumentParser(description="AI AURA live OTC monitor")
    parser.add_argument("--asset", default="EURUSD_otc")
    parser.add_argument("--period", type=int, default=1, help="subscribe period (s)")
    parser.add_argument("--refresh", type=float, default=1.0)
    parser.add_argument("--no-store", action="store_true", help="do not persist ticks")
    args = parser.parse_args()

    ssid = load_ssid()
    setup_logging(
        ssid,
        level=logging.INFO,
        log_file=PROJECT_ROOT / "logs" / "live_monitor.log",
        console=False,  # keep the terminal for the monitor UI
    )

    provider = PocketOptionMarketDataProvider(ssid)
    store = None if args.no_store else TickStore(PROJECT_ROOT / "data" / "raw" / "ticks")

    print("AI AURA — LIVE OTC\nConnecting...")
    provider.connect()
    provider.subscribe(args.asset, args.period)
    if store is not None:
        provider.add_tick_listener(store.append)

    started = time.time()
    last_price = None
    price_changes = 0
    try:
        while True:
            time.sleep(args.refresh)
            health = provider.health_check()
            tick = provider.get_latest_tick(args.asset)
            if tick is not None and last_price is not None and tick.price != last_price:
                price_changes += 1
            if tick is not None:
                last_price = tick.price

            if not health.connected:
                quality = "DOWN"
            elif health.last_tick_age_s is None or health.last_tick_age_s > 5:
                quality = "STALE"
            else:
                quality = "GOOD"

            lines = [
                "AI AURA — LIVE OTC",
                "",
                f"Asset:          {args.asset}",
                f"Price:          {tick.price if tick else '—'}",
                f"Last Tick:      {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(tick.source_timestamp)) + ' UTC' if tick else '—'}",
                f"Ticks Received: {health.ticks_received}",
                f"Price Changes:  {price_changes}",
                f"Latency (est):  {f'{tick.latency_ms:.0f} ms' if tick else '—'}",
                f"Connection:     {'CONNECTED' if health.connected else 'DISCONNECTED'}",
                f"Reconnects:     {health.reconnect_count}",
                f"Data Quality:   {quality}",
                f"Persisted:      {store.ticks_persisted if store else 'off'}",
                f"Uptime:         {int(time.time() - started)}s",
                "",
                "Ctrl+C to stop.",
            ]
            sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(lines) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if store is not None:
            provider.remove_tick_listener(store.append)
            store.close()
        provider.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
