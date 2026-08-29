"""Credential-safety tests: env loading and log redaction."""

import io
import json
import logging

import pytest

from services.market_data import security

FAKE_SESSION_DECODED = 'a:4:{s:10:"session_id";s:32:"0123456789abcdef0123456789abcdef";}hash123'
FAKE_SESSION_ESCAPED = json.dumps(FAKE_SESSION_DECODED)[1:-1]
FAKE_SSID = (
    '42["auth",{"session":"' + FAKE_SESSION_ESCAPED + '","isDemo":0,"uid":99887766,"platform":2}]'
)

# A realistic long PHP-serialized session — long enough that a 120-char
# truncation cuts through the MIDDLE of the value (the exact case that defeated
# exact-substring redaction and shipped the critical leak green).
LONG_SESSION_DECODED = (
    'a:4:{s:10:"session_id";s:32:"9f8e7d6c5b4a39281706fedcba098765";'
    's:10:"ip_address";s:13:"203.0.113.199";'
    's:10:"user_agent";s:64:"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36";'
    's:13:"last_activity";i:1788000000;}'
    'c0ffee1234567890abcdef1234567890abcdef12'
)
LONG_SESSION_ESCAPED = json.dumps(LONG_SESSION_DECODED)[1:-1]
LONG_SSID = (
    '42["auth",{"session":"' + LONG_SESSION_ESCAPED + '","isDemo":0,"uid":42,"platform":2}]'
)


def make_logger_with_filter(fragments):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(security.SecretRedactionFilter(fragments))
    log = logging.Logger("redaction-test")
    log.addHandler(handler)
    return log, stream


def test_derive_fragments_cover_all_representations():
    fragments = security.derive_secret_fragments(FAKE_SSID)
    assert FAKE_SSID in fragments
    assert FAKE_SESSION_DECODED in fragments
    assert FAKE_SESSION_ESCAPED in fragments


def test_redaction_removes_every_representation():
    fragments = security.derive_secret_fragments(FAKE_SSID)
    log, stream = make_logger_with_filter(fragments)

    # what the vendored client actually logs (rebuilt auth message)
    rebuilt = "42" + json.dumps(["auth", {"session": FAKE_SESSION_DECODED, "isDemo": 0}])
    log.info("Sending auth: %s...", rebuilt[:120])
    log.info("full ssid: %s", FAKE_SSID)
    log.info("decoded: " + FAKE_SESSION_DECODED)

    output = stream.getvalue()
    assert FAKE_SESSION_DECODED not in output
    assert FAKE_SESSION_ESCAPED not in output
    assert FAKE_SSID not in output
    assert security.REDACTED in output


def test_redaction_leaves_normal_messages_alone():
    log, stream = make_logger_with_filter(security.derive_secret_fragments(FAKE_SSID))
    log.info("subscribed to EURUSD_otc period=1")
    assert "subscribed to EURUSD_otc period=1" in stream.getvalue()


def _build_auth_msg(session_decoded: str) -> str:
    """Mirror the vendored client's _build_auth_message output."""
    return "42" + json.dumps(
        ["auth", {"session": session_decoded, "isDemo": 0, "uid": 42, "platform": 2}]
    )


def test_truncated_long_session_is_redacted():
    """The regression that shipped the critical leak: a long session logged
    truncated to 120 chars must still be fully redacted."""
    fragments = security.derive_secret_fragments(LONG_SSID)
    log, stream = make_logger_with_filter(fragments)

    auth_msg = _build_auth_msg(LONG_SESSION_DECODED)
    assert len(auth_msg) > 120  # ensure truncation actually cuts the session
    log.info("Sending auth: %s...", auth_msg[:120])  # what the OLD client did

    output = stream.getvalue()
    # No run of the session id / token may survive.
    assert "9f8e7d6c5b4a39281706fedcba098765" not in output
    # No 12+ char slice of the escaped session value survives either.
    window = auth_msg[24:120]  # the leaked region past the `"session":"` prefix
    for start in range(0, len(window) - 12):
        assert window[start : start + 12] not in output
    assert security.REDACTED in output


def test_scan_for_leaks_catches_truncated_session():
    auth_msg = _build_auth_msg(LONG_SESSION_DECODED)
    leaked_line = f"Sending auth: {auth_msg[:120]}..."
    indicators = security.scan_for_leaks(leaked_line, security.derive_secret_fragments(LONG_SSID))
    assert indicators, "scan must flag an unredacted truncated session"


def test_scan_for_leaks_clean_when_redacted():
    fragments = security.derive_secret_fragments(LONG_SSID)
    flt = security.SecretRedactionFilter(fragments)
    auth_msg = _build_auth_msg(LONG_SESSION_DECODED)
    redacted = flt.redact(f"Sending auth: {auth_msg[:120]}...")
    assert security.scan_for_leaks(redacted, fragments) == []


def test_load_ssid_missing_raises_with_instructions(monkeypatch, tmp_path):
    monkeypatch.delenv("PO_SSID", raising=False)
    with pytest.raises(security.MissingCredentialError) as excinfo:
        security.load_ssid(dotenv_path=tmp_path / "no.env")
    assert ".env" in str(excinfo.value)


def test_load_ssid_malformed_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PO_SSID", "not-an-auth-frame")
    with pytest.raises(security.MissingCredentialError):
        security.load_ssid(dotenv_path=tmp_path / "no.env")


def test_load_ssid_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("PO_SSID", FAKE_SSID)
    assert security.load_ssid(dotenv_path=tmp_path / "no.env") == FAKE_SSID
