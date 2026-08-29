"""Vendor integrity: the two AIAURA patches must stay present and marked."""

from pathlib import Path

VENDOR_CLIENT = (
    Path(__file__).resolve().parents[1]
    / "services" / "market_data" / "vendor" / "pocketoptionapi" / "ws" / "client.py"
)


def test_tls_patch_present():
    source = VENDOR_CLIENT.read_text(encoding="utf-8")
    assert "AIAURA-PATCH(tls)" in source
    assert 'os.environ.get("PO_TLS_INSECURE", "0") == "1"' in source


def test_stream_batch_patch_present():
    source = VENDOR_CLIENT.read_text(encoding="utf-8")
    assert "AIAURA-PATCH(stream)" in source
    assert "for stream_data in data:" in source


def test_auth_log_patch_present_and_no_value_logged():
    source = VENDOR_CLIENT.read_text(encoding="utf-8")
    assert "AIAURA-PATCH(log)" in source
    # the credential-leaking logging CALL must be gone (mention in the patch
    # comment is fine — check for the actual f-string log of the value)
    assert 'f"Sending auth: {auth_msg[:120]}...")' not in source
    assert 'logger.info("Sending auth message (%d bytes)", len(auth_msg))' in source


def test_vendor_notes_document_patches():
    notes = (VENDOR_CLIENT.parents[2] / "VENDOR_NOTES.md").read_text(encoding="utf-8")
    assert "AIAURA-PATCH(tls)" in notes
    assert "AIAURA-PATCH(stream)" in notes
    assert "AIAURA-PATCH(log)" in notes
    assert "5b7418a51f577732a4e7f7aeeecf969610bda3cb" in notes
