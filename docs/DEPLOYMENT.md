# Deployment

## Why NOT Vercel (or any serverless)

AI AURA's backend is a **long-lived process**: it holds a persistent WebSocket
to Pocket Option on background threads, keeps market state (candles,
subscriptions) in memory, and writes local storage (SQLite + Parquet).

Serverless platforms (Vercel, Netlify Functions, Lambda) run **short-lived,
stateless functions** that spin up per request and die. They cannot:

- keep a WebSocket / background event loop alive between requests,
- hold in-memory state,
- persist a local database/filesystem.

So the app **cannot** run on Vercel. This is a platform limitation, not a
configuration issue. Use one of the options below instead.

## What the app needs

A machine that **stays on** and runs one persistent Python process, with a
valid `PO_SSID` in its environment. That machine holds the live session.

## Option A — Home use over Wi-Fi (no cloud, no cost) — recommended

Run on your PC; open it from your iPhone on the same Wi-Fi.

```
.venv/Scripts/python scripts/serve.py
```

It prints `http://<your-pc-ip>:8000`. Open that on the iPhone → Share → **Add
to Home Screen** → fullscreen app. Keep the PC on while you use it.
No public exposure; nothing leaves your network.

## Option B — Access from anywhere via a tunnel (still runs on your PC)

A tunnel gives your local server a public HTTPS URL you can open on the iPhone
on cellular/any network. Cloudflare's quick tunnel needs **no account**:

```
# 1) install cloudflared once (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
# 2) run the app:
.venv/Scripts/python scripts/serve.py
# 3) in another terminal, tunnel to it:
cloudflared tunnel --url http://localhost:8000
```

cloudflared prints a `https://<random>.trycloudflare.com` URL — open it on the
iPhone.

**Security (important):** a public URL means anyone who has it can reach the API
that holds your live session. Before exposing publicly, set an access token:

```
# add to .env
AIAURA_TOKEN=some-long-random-string
```

When set, the API requires the token and the PWA asks for it once. Never expose
the app publicly without a token, and never commit the token.

## Option C — Always-on cloud (no PC needed)

Deploy the backend to a host that runs **persistent processes** (NOT Vercel):
Railway, Render, Fly.io, or any VPS. Steps (host-specific):

1. Push the repo to the host; start command:
   `python -m uvicorn apps.api.main:app --host 0.0.0.0 --port $PORT`
2. Set env vars on the host: `PO_SSID` (your captured auth frame) and
   `AIAURA_TOKEN` (a strong random string).
3. Ensure a persistent disk if you want `data/` to survive restarts.

**You** set `PO_SSID` on the host (it is your credential — never share it, never
put it in code). Caveats: Pocket Option may block datacenter IPs, and running a
personal session from the cloud may conflict with their terms. The SSID also
expires when your browser session ends — you must recapture and update it.

## Reality check

Hosting makes the app *reachable*; it does not make the *signal* good. The
current signal is an unvalidated baseline (see docs/APP.md). Do not risk real
money on it until an edge is measured (Phases 7/9/10/15) — and be prepared for
the honest possibility that no edge exists.
