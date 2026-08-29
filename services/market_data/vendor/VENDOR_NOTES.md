# Vendored library: pocketoptionapi

- **Upstream**: https://github.com/chema-creator/PocketOptionApi
- **Commit**: `5b7418a51f577732a4e7f7aeeecf969610bda3cb` (vendored 2026-08-29)
- **License**: MIT per upstream README (upstream repo ships no LICENSE file)
- **Why vendored** (instead of pip install from git):
  1. Pins the exact audited code — upstream can change or disappear at any time.
  2. Protects against supply-chain surprises (the audit in
     `docs/POCKET_OPTION_API_AUDIT.md` applies to this exact snapshot).
  3. Allows minimal, clearly-marked local patches.

## Local patches (marked `AIAURA-PATCH` in source)

| Patch | File | Reason |
|---|---|---|
| `AIAURA-PATCH(tls)` | `pocketoptionapi/ws/client.py` | Upstream disables TLS certificate verification, exposing the SSID session credential to MITM. Verification is now ON by default; `PO_TLS_INSECURE=1` restores upstream behavior as a last resort. |
| `AIAURA-PATCH(stream)` | `pocketoptionapi/ws/client.py` | Upstream processed only `data[0]` of an `updateStream` payload; a batched message would silently drop ticks. All rows are now processed. |
| `AIAURA-PATCH(log)` | `pocketoptionapi/ws/client.py` | Upstream logged `Sending auth: {auth_msg[:120]}...` at INFO, leaking ~96 chars of the live session (incl. `session_id`) in cleartext. A truncated credential defeats exact-match log redaction by construction, so the value is no longer logged — only its byte length. (Defense in depth: `security.SecretRedactionFilter` also structurally redacts any `"session"` field value, truncated or not.) |

Everything else is byte-identical to the upstream commit. Do not edit vendored
code without adding an `AIAURA-PATCH` marker and a row in this table.

## Order-execution code (present but NEVER used)

The upstream library includes order-execution paths (`PocketOption.buy`,
`check_win`, `get_order_result`, `Buyv3`, `Buyv3_by_raw_expired`, the
`openOrder` wire message). AI AURA is read-only by design: no code outside
this vendor directory may reference them. `tests/test_no_order_execution.py`
enforces this automatically.
