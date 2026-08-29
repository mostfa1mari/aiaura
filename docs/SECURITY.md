# Security

## The credential: PO_SSID

The Pocket Option SSID is a **live session credential** — anyone holding it
can act as the account. Treat it like a password.

Rules (all enforced or automated where possible):

| Rule | Enforcement |
|---|---|
| Lives only in `.env` (or the process environment) | `security.load_ssid()` is the only read path |
| Never committed | `.gitignore`: `.env`, `.env.*` (only `.env.example` allowed) |
| Never pasted into chat / docs / source | policy; `.env.example` instructs the user |
| Never logged | Value removed at the source (`AIAURA-PATCH(log)`); plus `SecretRedactionFilter` on every log handler (`security.setup_logging`) which *structurally* redacts any `"session"` field value even when truncated/re-escaped; unit-tested against a realistic long session; every live validation run re-scans its own log with the same truncation-proof `scan_for_leaks` check |
| Never sent to third parties | the vendored client talks only to `*.po.market` (verified in audit); no other network code exists |
| Never exposed to a frontend | the PWA (Phase 18) will talk to AI AURA's own API only |
| Protected in transit | TLS certificate verification enabled by default (`AIAURA-PATCH(tls)`; upstream had it disabled) |

Known upstream leak that motivated all this: the vendored client logged the
first 120 chars of the auth message at INFO level — a *truncated* fragment that
exact-value redaction cannot match. It is now fixed at the source
(`AIAURA-PATCH(log)`), and the structural `"session"`-field redaction is the
safety net for any other path. The redaction filter must still be installed
**before** `connect()` — `setup_logging(ssid, ...)` does this; both scripts use
it.

## Rotating / revoking

The SSID expires when the browser session ends or Pocket Option invalidates
it. On auth failure the provider marks the state terminal and stops retrying
(no hammering). Recovery: log in again in the browser, capture a fresh frame,
update `.env`, restart.

## Read-only guarantee

No AI AURA code may reference the library's order-execution surface
(`buy`, `buyv3`, `Buyv3`, `openOrder`, `check_win`, `get_order_result`, ...).
`tests/test_no_order_execution.py` AST-scans all non-vendor Python on every
test run; the live validation additionally asserts the order state stayed
untouched at runtime.

## Secret scanning

- `.gitignore` blocks the `.env` family and all `data/`/`logs/` output.
- Before any commit that touches configuration, run the test suite (the
  redaction and no-order tests are part of it).
- Recommended (future CI): add `gitleaks` or `trufflehog` once a remote and
  CI pipeline exist (Phase 22 hardening).
