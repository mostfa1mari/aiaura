-- AI AURA — Supabase / Postgres schema.
-- The worker auto-creates this on startup, so running it by hand is optional.
-- To run manually: Supabase dashboard -> SQL Editor -> paste -> Run.

CREATE TABLE IF NOT EXISTS predictions (
    signal_id      TEXT PRIMARY KEY,
    created_at     DOUBLE PRECISION NOT NULL,
    asset          TEXT NOT NULL,
    expiry_s       INTEGER NOT NULL,
    signal         TEXT NOT NULL,               -- BUY | SELL
    score          DOUBLE PRECISION,
    strength       DOUBLE PRECISION,
    agreement      DOUBLE PRECISION,
    regime         TEXT,
    data_sufficiency DOUBLE PRECISION,
    entry_price    DOUBLE PRECISION,
    market_ts      DOUBLE PRECISION,
    prediction_latency_ms DOUBLE PRECISION,
    model_version  TEXT,
    context_json   TEXT,                          -- sub-signals etc. (audit)
    result         TEXT,                          -- WIN | LOSS | NULL (pending)
    result_at      DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_pred_created ON predictions(created_at);
CREATE INDEX IF NOT EXISTS idx_pred_asset   ON predictions(asset);
CREATE INDEX IF NOT EXISTS idx_pred_result  ON predictions(result);

-- Connection string to give the worker as DATABASE_URL:
--   Supabase dashboard -> Project Settings -> Database -> Connection string
--   Use the "Session"/pooler URI, URL-encode the password, and append sslmode=require, e.g.:
--   postgresql://postgres:<PW>@db.<ref>.supabase.co:5432/postgres?sslmode=require
