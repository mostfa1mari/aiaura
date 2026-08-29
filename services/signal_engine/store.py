"""Prediction & outcome store.

Two backends behind one interface:
  * SqliteSignalStore  — local file (default; dev / single machine).
  * PostgresSignalStore — Supabase / any Postgres (cloud, persistent).

``make_store()`` picks Postgres when DATABASE_URL is set, else SQLite. The
schema and stats are honest: only settled (WIN/LOSS) predictions count toward
win rate; pending are reported separately.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_COLUMNS = (
    "signal_id, created_at, asset, expiry_s, signal, score, strength, agreement, "
    "regime, data_sufficiency, entry_price, market_ts, prediction_latency_ms, "
    "model_version, context_json, result, result_at"
)


def _new_row(asset, expiry_s, signal, score, strength, agreement, regime,
             data_sufficiency, entry_price, market_ts, prediction_latency_ms,
             model_version, context) -> tuple:
    return (
        uuid.uuid4().hex, time.time(), asset, int(expiry_s), signal, score, strength,
        agreement, regime, data_sufficiency, entry_price, market_ts,
        prediction_latency_ms, model_version, json.dumps(context), None, None,
    )


# ----------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    signal_id      TEXT PRIMARY KEY,
    created_at     REAL NOT NULL,
    asset          TEXT NOT NULL,
    expiry_s       INTEGER NOT NULL,
    signal         TEXT NOT NULL,
    score          REAL, strength REAL, agreement REAL,
    regime         TEXT, data_sufficiency REAL,
    entry_price    REAL, market_ts REAL, prediction_latency_ms REAL,
    model_version  TEXT, context_json TEXT,
    result         TEXT, result_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pred_created ON predictions(created_at);
CREATE INDEX IF NOT EXISTS idx_pred_asset   ON predictions(asset);
CREATE INDEX IF NOT EXISTS idx_pred_result  ON predictions(result);
"""


class SqliteSignalStore:
    backend = "sqlite"

    def __init__(self, db_path: Path | str = "data/aiaura.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SQLITE_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_prediction(self, **kw) -> str:
        row = _new_row(**kw)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO predictions ({_COLUMNS}) VALUES ({','.join('?' * 17)})", row
            )
            self._conn.commit()
        return row[0]

    def record_result(self, signal_id: str, result: str) -> bool:
        result = result.upper()
        if result not in ("WIN", "LOSS"):
            raise ValueError("result must be WIN or LOSS")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE predictions SET result=?, result_at=? WHERE signal_id=? AND result IS NULL",
                (result, time.time(), signal_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_prediction(self, signal_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM predictions WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 50) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
            wins = self._conn.execute("SELECT COUNT(*) FROM predictions WHERE result='WIN'").fetchone()[0]
            losses = self._conn.execute("SELECT COUNT(*) FROM predictions WHERE result='LOSS'").fetchone()[0]
            pending = self._conn.execute("SELECT COUNT(*) FROM predictions WHERE result IS NULL").fetchone()[0]
            by_asset = self._conn.execute(
                """SELECT asset, SUM(result='WIN') AS wins, SUM(result='LOSS') AS losses,
                          SUM(result IS NULL) AS pending
                   FROM predictions GROUP BY asset ORDER BY (wins+losses) DESC"""
            ).fetchall()
            by_expiry = self._conn.execute(
                """SELECT expiry_s, SUM(result='WIN') AS wins, SUM(result='LOSS') AS losses
                   FROM predictions GROUP BY expiry_s ORDER BY expiry_s"""
            ).fetchall()
            settled_rows = self._conn.execute(
                "SELECT result FROM predictions WHERE result IS NOT NULL ORDER BY result_at"
            ).fetchall()
        results = [r["result"] for r in settled_rows]
        return _build_stats(total, wins, losses, pending,
                            [dict(r) for r in by_asset], [dict(r) for r in by_expiry], results)


# ----------------------------------------------------------------------
# Postgres / Supabase
# ----------------------------------------------------------------------

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    signal_id      TEXT PRIMARY KEY,
    created_at     DOUBLE PRECISION NOT NULL,
    asset          TEXT NOT NULL,
    expiry_s       INTEGER NOT NULL,
    signal         TEXT NOT NULL,
    score          DOUBLE PRECISION, strength DOUBLE PRECISION, agreement DOUBLE PRECISION,
    regime         TEXT, data_sufficiency DOUBLE PRECISION,
    entry_price    DOUBLE PRECISION, market_ts DOUBLE PRECISION,
    prediction_latency_ms DOUBLE PRECISION,
    model_version  TEXT, context_json TEXT,
    result         TEXT, result_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_pred_created ON predictions(created_at);
CREATE INDEX IF NOT EXISTS idx_pred_asset   ON predictions(asset);
CREATE INDEX IF NOT EXISTS idx_pred_result  ON predictions(result);
"""


class PostgresSignalStore:
    backend = "postgres"

    def __init__(self, dsn: str):
        import psycopg  # lazy import so SQLite users need no driver

        self._psycopg = psycopg
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(_PG_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _reconnect_if_needed(self):
        if self._conn.closed:
            self._conn = self._psycopg.connect(self._conn.info.dsn, autocommit=True)

    def record_prediction(self, **kw) -> str:
        row = _new_row(**kw)
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO predictions ({_COLUMNS}) VALUES ({','.join(['%s'] * 17)})", row
                )
        return row[0]

    def record_result(self, signal_id: str, result: str) -> bool:
        result = result.upper()
        if result not in ("WIN", "LOSS"):
            raise ValueError("result must be WIN or LOSS")
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE predictions SET result=%s, result_at=%s WHERE signal_id=%s AND result IS NULL",
                    (result, time.time(), signal_id),
                )
                return cur.rowcount > 0

    def _fetch_dicts(self, sql, params=()) -> List[dict]:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_prediction(self, signal_id: str) -> Optional[dict]:
        rows = self._fetch_dicts("SELECT * FROM predictions WHERE signal_id=%s", (signal_id,))
        return rows[0] if rows else None

    def recent(self, limit: int = 50) -> List[dict]:
        return self._fetch_dicts(
            "SELECT * FROM predictions ORDER BY created_at DESC LIMIT %s", (limit,))

    def stats(self) -> dict:
        agg = self._fetch_dicts(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE result='WIN') AS wins,
                      COUNT(*) FILTER (WHERE result='LOSS') AS losses,
                      COUNT(*) FILTER (WHERE result IS NULL) AS pending
               FROM predictions""")[0]
        by_asset = self._fetch_dicts(
            """SELECT asset,
                      COUNT(*) FILTER (WHERE result='WIN') AS wins,
                      COUNT(*) FILTER (WHERE result='LOSS') AS losses,
                      COUNT(*) FILTER (WHERE result IS NULL) AS pending
               FROM predictions GROUP BY asset
               ORDER BY (COUNT(*) FILTER (WHERE result='WIN')
                         + COUNT(*) FILTER (WHERE result='LOSS')) DESC""")
        by_expiry = self._fetch_dicts(
            """SELECT expiry_s,
                      COUNT(*) FILTER (WHERE result='WIN') AS wins,
                      COUNT(*) FILTER (WHERE result='LOSS') AS losses
               FROM predictions GROUP BY expiry_s ORDER BY expiry_s""")
        settled = self._fetch_dicts(
            "SELECT result FROM predictions WHERE result IS NOT NULL ORDER BY result_at")
        return _build_stats(agg["total"], agg["wins"], agg["losses"], agg["pending"],
                            by_asset, by_expiry, [r["result"] for r in settled])


# ----------------------------------------------------------------------
# Shared stats logic + factory
# ----------------------------------------------------------------------

def _max_losing_streak(results: List[str]) -> int:
    streak = worst = 0
    for r in results:
        if r == "LOSS":
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    return worst


def _build_stats(total, wins, losses, pending, by_asset, by_expiry, settled_results) -> dict:
    settled = wins + losses
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "settled": settled,
        "win_rate": (wins / settled) if settled else None,
        "max_losing_streak": _max_losing_streak(settled_results),
        "by_asset": by_asset,
        "by_expiry": by_expiry,
        "disclaimer": (
            "Win rate is over user-reported settled signals only; sample may be "
            "tiny and unrepresentative. Baseline heuristic, not a validated edge. "
            "No guarantee of future performance."
        ),
    }


def make_store(db_path: Path | str = "data/aiaura.db"):
    """Postgres when DATABASE_URL (or SUPABASE_DB_URL) is set, else SQLite."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if dsn:
        return PostgresSignalStore(dsn.strip())
    return SqliteSignalStore(db_path)


# Backwards-compatible alias (tests and existing callers).
SignalStore = SqliteSignalStore
