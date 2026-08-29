"""AI AURA launcher — serve the app so your iPhone can open it.

Prints the exact URL to open on your phone and runs the API (which hosts the
PWA). By default it binds to your local network (Wi-Fi) — open the printed
http://<your-ip>:PORT on an iPhone on the SAME Wi-Fi, then "Add to Home
Screen" for a fullscreen app.

    .venv/Scripts/python scripts/serve.py                # LAN (home Wi-Fi)
    .venv/Scripts/python scripts/serve.py --port 8000

Anywhere-access (outside home) needs a public tunnel or a persistent host —
see docs/DEPLOYMENT.md. When exposing publicly, set AIAURA_TOKEN in .env so the
API requires a token (the PWA will ask for it once).

This app must run on a machine that stays on and keeps the live Pocket Option
session — it cannot run on serverless platforms like Vercel (persistent
WebSocket + background threads + local state).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def lan_ip() -> str:
    """Best-effort local network IP (no packets actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve AI AURA for phone use")
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (0.0.0.0 = reachable on your LAN)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    ip = lan_ip()
    token_set = bool((os.environ.get("AIAURA_TOKEN") or "").strip())

    print("=" * 56)
    print("  AI AURA — open this on your iPhone (same Wi-Fi):")
    print(f"      http://{ip}:{args.port}")
    print("  Then: Share → Add to Home Screen → open fullscreen.")
    print("-" * 56)
    print(f"  Access token: {'ON (AIAURA_TOKEN set)' if token_set else 'OFF (LAN only — do NOT expose publicly)'}")
    print("  Anywhere-access / always-on: see docs/DEPLOYMENT.md")
    print("=" * 56)

    import uvicorn

    uvicorn.run("apps.api.main:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
