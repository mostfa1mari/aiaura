# Deployment

## Why NOT Vercel (for the backend)

AI AURA's backend is a **long-lived process**: it holds a persistent WebSocket
to Pocket Option on background threads, keeps market state in memory, and needs
persistent storage. Serverless platforms (Vercel, Netlify, Lambda) run
**short-lived, stateless functions** that die after each request — they cannot
hold a WebSocket open, keep in-memory state, or persist a local filesystem. So
the backend **cannot** run on Vercel. Vercel can host the **frontend** (static
PWA) only.

## The always-on architecture (Vercel + Supabase + worker)

```
[ Worker ]  Railway / Render / Fly / VPS   ← the one always-on piece
  • holds the Pocket Option WebSocket (live ticks)
  • builds candles + generates BUY/SELL
  • writes to  ↓                             ↑ API (token-protected, CORS)
[ Supabase Postgres ]  predictions/outcomes     |
                                                 |
[ Vercel ]  static PWA  ── user sets the worker URL + token in the app's ⚙ Settings
```

The PWA is server-agnostic: open **⚙ Settings** in the app, enter your worker
URL and access token — it stores them and talks to your worker directly (CORS +
bearer token). Leave the URL empty to use whatever site served the app (i.e.
when the worker serves the PWA itself).

---

## Step-by-step

### 1. Supabase (database)
1. Create a project at supabase.com.
2. Project Settings → Database → **Connection string** → copy the URI, put your
   password in, append `?sslmode=require`. This is your `DATABASE_URL`.
   (The worker auto-creates the table on first boot; `infra/supabase_schema.sql`
   is provided if you prefer to run it by hand.)

### 2. Worker (Railway — easiest)
1. Push this repo to GitHub. On railway.app → **New Project → Deploy from GitHub**.
   Railway detects the `Dockerfile` (config in `railway.json`).
2. In the service **Variables**, set:
   - `PO_SSID` = your captured auth frame (see `.env.example`). **You** paste
     this — it is your credential; it never goes in code or git.
   - `AIAURA_TOKEN` = a long random string (protects the public API).
   - `DATABASE_URL` = the Supabase URI from step 1.
   - `ALLOWED_ORIGINS` = your Vercel URL (optional; default `*` is fine because
     auth is by token, not cookies).
3. Deploy. Railway gives a public URL like `https://aiaura-xxxx.up.railway.app`.
   Check `https://.../api/health` → `{"connected": true, ...}`.

(Render is equivalent — `render.yaml` is included. Use the **Starter** plan or
higher; the Free plan sleeps and would drop the WebSocket.)

### 3. Frontend (Vercel)
1. On vercel.com → **New Project → import this repo**. `vercel.json` serves the
   PWA from `apps/web` (no build step).
2. Deploy → you get `https://your-app.vercel.app`.
3. Open it on your iPhone → **⚙ Settings** → enter the worker URL
   (`https://aiaura-xxxx.up.railway.app`) and your `AIAURA_TOKEN` → Save.
4. **Add to Home Screen** → fullscreen native app, reachable from anywhere.

---

## Simpler alternatives

- **One host, everything:** skip Vercel/Supabase — deploy just the worker
  (Railway/Render); it serves the PWA too and uses SQLite on a persistent disk.
  One URL, less plumbing. Good for a single user.
- **Home, no cloud:** `.venv/Scripts/python scripts/serve.py` on your PC, open
  the printed URL on your iPhone (same Wi-Fi). Free; nothing exposed. Add a
  Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:8000`) for
  anywhere-access — set `AIAURA_TOKEN` first.

## Security

- Never expose the app publicly without `AIAURA_TOKEN` set.
- `PO_SSID` is your live session — set it only in the host's env, never in code
  or git. It **expires** when your browser session ends or Pocket Option rotates
  it; when it does, the app stops until you capture a fresh frame
  (`scripts/check_ssid.py` verifies it) and update the host variable.
- Pocket Option may block datacenter IPs; if the cloud worker can't connect,
  use the home + tunnel option (residential IP).

## Reality check

Hosting makes the app *reachable*, not *profitable*. The current signal is an
unvalidated baseline (docs/APP.md). Do not risk real money on it before an edge
is measured (Phases 7/9/10/15) — and be ready for the honest possibility that
no edge exists.
