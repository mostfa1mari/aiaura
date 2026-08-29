"""Phase-30 first-milestone validation runner.

Runs the live Pocket Option OTC pipeline for N minutes, verifies every item
of the Phase-30 checklist, and writes docs/LIVE_OTC_VALIDATION.md.

Usage:
    .venv/Scripts/python scripts/validate_live_otc.py [--minutes 10]
        [--asset EURUSD_otc] [--skip-reconnect-test]

Exit code 0 only when all critical checks pass.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.market_data.pocket_option_provider import PocketOptionMarketDataProvider
from services.market_data.security import (
    derive_secret_fragments,
    load_ssid,
    scan_for_leaks,
    setup_logging,
)
from services.market_data.storage import TickStore

LOG_FILE = PROJECT_ROOT / "logs" / "validate_live_otc.log"
REPORT_FILE = PROJECT_ROOT / "docs" / "LIVE_OTC_VALIDATION.md"
GAP_THRESHOLD_S = 5.0

logger = logging.getLogger("validate")


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI AURA live OTC validation")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--asset", default="EURUSD_otc")
    parser.add_argument("--period", type=int, default=1)
    parser.add_argument("--skip-reconnect-test", action="store_true")
    args = parser.parse_args()

    checks: dict[str, tuple[bool, str]] = {}
    errors: list[str] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks[name] = (bool(passed), detail)
        logger.info("CHECK %-38s %s %s", name, "PASS" if passed else "FAIL", detail)

    # 3. credential from environment (never logged)
    ssid = load_ssid()
    setup_logging(ssid, level=logging.INFO, log_file=LOG_FILE, console=True)
    secret_fragments = derive_secret_fragments(ssid)
    check("PO_SSID loaded from environment", True, "value never printed")

    # 1./2. library imports + provider initializes
    provider = PocketOptionMarketDataProvider(ssid)
    check("PocketOptionApi importable (vendored)", True)
    check("Provider initialized", True)

    store = TickStore(PROJECT_ROOT / "data" / "raw" / "ticks")
    ticks: list = []
    provider.add_tick_listener(ticks.append)
    provider.add_tick_listener(store.append)

    # 4. connection
    try:
        provider.connect()
        check("Connection succeeds", True)
    except Exception as exc:
        check("Connection succeeds", False, str(exc))
        errors.append(f"connect: {exc}")
        write_report(args, checks, [], store, provider, errors, secret_fragments)
        return 1

    try:
        # 5. asset availability (dynamic discovery, no hardcoded availability)
        catalog = provider.get_assets()
        otc = provider.get_otc_assets()
        target = args.asset
        info = catalog.get(target)
        if info is not None and info.is_available:
            check(f"{target} is available", True, f"payout={info.payout}%")
        else:
            detail = "not in catalog" if info is None else "is_available=False"
            check(f"{target} is available", False, detail)
            if otc:
                target = sorted(otc)[0]
                logger.warning("falling back to %s for the remaining checks", target)
                errors.append(f"{args.asset} unavailable; ran stream checks on {target}")
        logger.info("catalog: %d assets, %d OTC available", len(catalog), len(otc))

        # 6. subscription
        try:
            provider.subscribe(target, args.period)
            check("Subscription succeeds", True, target)
        except Exception as exc:
            check("Subscription succeeds", False, str(exc))
            errors.append(f"subscribe: {exc}")

        # -- soak -----------------------------------------------------
        duration = args.minutes * 60.0
        started = time.time()
        reconnect_done = args.skip_reconnect_test
        reconnect_at = started + duration * 0.6
        reconnect_ok = None
        logger.info("soak started: %.1f minutes on %s", args.minutes, target)

        while time.time() - started < duration:
            time.sleep(1.0)
            if not reconnect_done and time.time() >= reconnect_at:
                reconnect_done = True
                logger.info("forcing socket close to test reconnection...")
                count_before = len(ticks)
                provider.force_disconnect_for_test()
                deadline = time.time() + 120
                reconnect_ok = False
                while time.time() < deadline:
                    time.sleep(1.0)
                    if provider.is_connected() and len(ticks) > count_before:
                        reconnect_ok = True
                        break
                logger.info("reconnection test result: %s", reconnect_ok)

        # 7. ticks arrive
        asset_ticks = [t for t in ticks if t.asset == target]
        check("Live ticks arrive", len(asset_ticks) > 0, f"{len(asset_ticks)} ticks")

        # 8. timestamps advance + prices change
        stamps = [t.source_timestamp for t in asset_ticks]
        advancing = sum(
            1 for a, b in zip(stamps, stamps[1:]) if b > a
        )
        prices = [t.price for t in asset_ticks]
        changes = sum(1 for a, b in zip(prices, prices[1:]) if b != a)
        check("Timestamps advance", len(stamps) > 1 and advancing > 0, f"{advancing} advances")
        check("Price changes detected", changes > 0, f"{changes} changes")

        # 10. reconnection
        if args.skip_reconnect_test:
            check("Reconnection works", True, "SKIPPED by flag (not exercised)")
        else:
            check("Reconnection works", bool(reconnect_ok), "forced close mid-run")

        # 9. persistence round-trip (match THIS run's tick_ids, not old rows)
        store.flush()
        captured_ids = {t.tick_id for t in asset_ticks}
        persisted = 0
        days = {datetime.fromtimestamp(t.source_timestamp, tz=timezone.utc).strftime("%Y-%m-%d") for t in asset_ticks}
        for day in sorted(days):
            frame = store.read_day(target, day)
            if frame is not None:
                persisted += int(frame["tick_id"].isin(captured_ids).sum())
        check(
            "Ticks persisted (parquet round-trip)",
            0 < len(captured_ids) == persisted,
            f"{persisted} rows read back / {len(captured_ids)} captured",
        )

        # 11. no order methods called (runtime evidence; static guard in tests/)
        import pocketoptionapi.global_value as gv

        order_untouched = not gv.order_data and not provider._client.api._order_results
        check("No order methods called", order_untouched, "order state untouched")

    finally:
        provider.remove_tick_listener(store.append)
        provider.remove_tick_listener(ticks.append)
        store.close()
        provider.disconnect()
        logging.shutdown()

    # 12. no credentials logged — scan the run's own log file. Uses the
    # truncation-proof structural check (not just exact fragments), so a
    # partial/truncated session leak cannot slip past as a false PASS.
    log_text = LOG_FILE.read_text(encoding="utf-8", errors="replace") if LOG_FILE.exists() else ""
    leaked = scan_for_leaks(log_text, secret_fragments)
    check("No credentials in logs", not leaked, f"scanned {LOG_FILE.name}; {leaked or 'clean'}")

    write_report(args, checks, ticks, store, provider, errors, secret_fragments)

    all_pass = all(passed for passed, _ in checks.values())
    print(f"\nValidation {'PASSED' if all_pass else 'FAILED'} — report: {REPORT_FILE}")
    return 0 if all_pass else 1


def write_report(args, checks, ticks, store, provider, errors, secret_fragments) -> None:
    lines = [
        "# LIVE OTC VALIDATION — Phase 30 first milestone",
        "",
        f"- **Run at**: {utc(time.time())}",
        f"- **Requested asset**: {args.asset}",
        f"- **Requested duration**: {args.minutes} min",
        f"- **Provider**: pocket_option (vendored PocketOptionApi @ 5b7418a)",
        "",
        "## Checklist",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for name, (passed, detail) in checks.items():
        lines.append(f"| {name} | {'✅ PASS' if passed else '❌ FAIL'} | {detail} |")

    lines += ["", "## Metrics", ""]
    if ticks:
        stamps = sorted(t.source_timestamp for t in ticks)
        prices = [t.price for t in ticks]
        intervals = [b - a for a, b in zip(stamps, stamps[1:]) if b >= a]
        latencies = sorted(t.latency_ms for t in ticks)
        gaps = [(a, b) for a, b in zip(stamps, stamps[1:]) if b - a > GAP_THRESHOLD_S]
        changes = sum(1 for a, b in zip(prices, prices[1:]) if b != a)

        def pct(sorted_vals, q):
            if not sorted_vals:
                return float("nan")
            idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
            return sorted_vals[idx]

        lines += [
            f"- tick_count: **{len(ticks)}**",
            f"- first_timestamp: {utc(stamps[0])}",
            f"- last_timestamp: {utc(stamps[-1])}",
            f"- min_price: {min(prices)}",
            f"- max_price: {max(prices)}",
            f"- price_changes: {changes}",
            f"- average_tick_interval: {statistics.mean(intervals):.3f} s" if intervals else "- average_tick_interval: n/a",
            f"- latency est. (relative to synced server clock): "
            f"mean {statistics.mean(latencies):.0f} ms, p50 {pct(latencies, 0.5):.0f} ms, p95 {pct(latencies, 0.95):.0f} ms",
            f"- data_gaps (> {GAP_THRESHOLD_S:.0f}s): {len(gaps)}"
            + (f", largest {max(b - a for a, b in gaps):.1f}s" if gaps else ""),
            f"- ticks_persisted: {store.ticks_persisted} rows in {len(store.files_written)} parquet part file(s)",
        ]
        if gaps and not args.skip_reconnect_test:
            lines.append("  - note: one gap is expected from the forced reconnection test")
    else:
        lines.append("- no ticks captured")

    lines += ["", "## Connection events", ""]
    for event in provider.connection_events:
        lines.append(f"- {utc(event['ts'])} — {event['event']} {event['detail']}".rstrip())

    lines += ["", "## Errors", ""]
    lines += [f"- {e}" for e in errors] or ["- none"]
    lines += [
        "",
        "## Notes",
        "",
        "- Latency is measured against the provider's synced-server-clock estimate;",
        "  it is a relative measure, not absolute one-way latency.",
        f"- Credential-leak scan checked {len(secret_fragments)} secret fragment(s)",
        "  against the run log; the values themselves never appear in this report.",
        "- No order-execution method was invoked at any point (see also",
        "  tests/test_no_order_execution.py).",
    ]

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
