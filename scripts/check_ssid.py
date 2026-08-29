"""Safely verify the PO_SSID in .env WITHOUT revealing its value.

Prints only structural facts and a clear verdict. Never prints the credential
or any part of the session. Run after pasting a new frame into .env:

    .venv/Scripts/python scripts/check_ssid.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.market_data.security import MissingCredentialError, load_ssid  # noqa: E402


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    raw = os.environ.get("PO_SSID")
    if not raw:
        print("PO_SSID: NOT SET (add it to .env — see .env.example)")
        return 1

    s = raw.strip().strip("'\"").strip()
    is_auth = s.startswith('42["auth"')
    has_session = '"session"' in s
    has_isdemo = "isDemo" in s
    has_uid = "uid" in s
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    mode = "REAL" if re.search(r'"isDemo"\s*:\s*0', s) else (
        "DEMO" if re.search(r'"isDemo"\s*:\s*1', s) else "unknown")

    print("PO_SSID structural check (no value shown):")
    print(f"  length:                 {len(s)}")
    print(f"  starts with 42[\"auth\"]:  {is_auth}")
    print(f"  has \"session\" field:     {has_session}")
    print(f"  has isDemo field:        {has_isdemo}")
    print(f"  has uid field:           {has_uid}")
    print(f"  account mode:            {mode}")
    print(f"  non-ASCII chars (emoji): {non_ascii}")

    try:
        load_ssid(dotenv_path=PROJECT_ROOT / ".env")
    except MissingCredentialError as exc:
        print("\nVERDICT: INVALID")
        print("reason:", str(exc).splitlines()[0])
        if not is_auth or not has_session:
            print(
                "\nYou likely copied a different WebSocket frame. You need the "
                'one the browser SENDS that starts exactly with 42["auth",'
                '{"session":"..."}. In DevTools > Network > WS > Messages, type '
                "'auth' in the filter box and copy that sent (green ↑) frame."
            )
        return 1

    print("\nVERDICT: VALID format. Ready for a live connection test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
