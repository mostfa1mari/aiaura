"""Credential handling: environment loading and log redaction.

Rules (docs/SECURITY.md):
  - The SSID is read ONLY from the environment / .env file.
  - It must never be printed, logged, committed, or sent anywhere except the
    Pocket Option WebSocket handshake performed by the vendored client.
  - The vendored library logs part of the auth message at INFO level, so a
    redaction filter is mandatory on every log handler (see
    ``install_secret_redaction`` / ``setup_logging``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional

REDACTED = "***REDACTED***"
_MIN_SECRET_LEN = 8

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Structural redaction of the credential's `session` field, independent of any
# registered secret value. Matches `"session"<:>"<value>` and consumes the
# value (honoring JSON `\"` escapes) up to its closing quote OR end of string.
# This is what defends against a *truncated* auth line (the vendored client
# used to log only a 120-char prefix, which exact-value matching can never
# catch). Applied to every log record and to post-run leak scans.
_SESSION_FIELD_RE = re.compile(r'("session"\s*:\s*\\?")((?:[^"\\]|\\.)*)')


class MissingCredentialError(Exception):
    """PO_SSID is absent or malformed. The message never contains the value."""


class SecretRedactionFilter(logging.Filter):
    """Replaces registered secret substrings in every record it sees.

    Attach to *handlers* (not loggers): handler filters run for records from
    every logger in the process, logger filters only for their own records.
    """

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        # Longest first so fragments of a longer secret don't leave residue.
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= _MIN_SECRET_LEN},
            key=len,
            reverse=True,
        )

    def redact(self, text: str) -> str:
        # Structural pass first: kills the session value even when truncated
        # or re-escaped, i.e. when exact-value matching cannot fire.
        text = _SESSION_FIELD_RE.sub(lambda m: m.group(1) + REDACTED, text)
        # Then exact known-value pass for any other representation.
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = self.redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:
            # Never let redaction break logging; but do not leak either:
            record.msg = REDACTED
            record.args = ()
        return True


def derive_secret_fragments(ssid: str) -> List[str]:
    """All representations of the credential that could appear in logs.

    Covers: the full SSID string, the raw captured session substring, its
    JSON-decoded form, and its re-encoded (escaped) form — the vendored
    client logs the rebuilt auth message, which uses the re-encoded form.
    """
    fragments = {ssid}
    match = re.search(r'"session"\s*:\s*"(.*)",\s*"isDemo"', ssid)
    if match:
        raw = match.group(1)
        fragments.add(raw)
        try:
            decoded = json.loads('"' + raw + '"')
            fragments.add(decoded)
            fragments.add(json.dumps(decoded)[1:-1])  # re-escaped, no quotes
        except (json.JSONDecodeError, ValueError):
            pass
    return [f for f in fragments if f and len(f) >= _MIN_SECRET_LEN]


def scan_for_leaks(text: str, secrets: Iterable[str] = ()) -> List[str]:
    """Return human-readable indicators of any credential leak in ``text``.

    Detects both exact registered fragments AND any un-redacted ``session``
    field value (the truncation-proof structural check). Empty list == clean.
    Used by the live-validation gate so it cannot report a false PASS on a
    partial/truncated leak.
    """
    indicators: List[str] = []
    for match in _SESSION_FIELD_RE.finditer(text):
        value = match.group(2)
        if value and value != REDACTED:
            indicators.append(f'unredacted "session" value ({len(value)} chars)')
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN and secret in text:
            indicators.append(f"registered secret fragment ({len(secret)} chars)")
    return indicators


def install_secret_redaction(secrets: Iterable[str]) -> SecretRedactionFilter:
    """Attach a redaction filter to all current root handlers."""
    flt = SecretRedactionFilter(secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(flt)
    return flt


def setup_logging(
    ssid: Optional[str],
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    console: bool = True,
) -> None:
    """Configure logging with mandatory secret redaction.

    Call this BEFORE connecting a provider so the vendored library's own
    log lines (including its auth-message INFO line) are redacted.
    """
    root = logging.getLogger()
    root.setLevel(level)
    handlers: List[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler())
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)

    if ssid:
        install_secret_redaction(derive_secret_fragments(ssid))


def load_ssid(dotenv_path: Optional[Path] = None) -> str:
    """Load PO_SSID from the environment (with .env support).

    Raises MissingCredentialError with setup instructions (and never the
    value itself) when absent or malformed.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path or PROJECT_ROOT / ".env")
    except ImportError:
        pass  # plain environment variables still work

    ssid = (os.environ.get("PO_SSID") or "").strip().strip("'\"").strip()
    if not ssid:
        raise MissingCredentialError(
            "PO_SSID is not set. Copy .env.example to .env in the project root "
            "and put your captured Pocket Option auth string there (see the "
            "instructions inside .env.example). Never paste it into chat, "
            "logs, or source code."
        )
    if not (ssid.startswith('42["auth"') and '"session"' in ssid):
        raise MissingCredentialError(
            'PO_SSID is set but malformed: expected the full frame starting '
            'with 42["auth",{"session":... exactly as captured from the '
            "browser WebSocket. Re-copy it without truncating or re-escaping."
        )
    return ssid
