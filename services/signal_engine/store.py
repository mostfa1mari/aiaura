"""Prediction & outcome store (SQLite).

Records every signal AI AURA shows and the WIN/LOSS the user reports back, plus
the feature/strategy context needed for later post-trade analysis and model
training (Phase 14). SQLite is appropriate here: transactional records with
dashboard queries, not high-rate raw data (that stays in Parquet).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    signal_id      TEXT PRIMARY KEY,
    created_at     REAL NOT NULL,
    asset          TEXT NOT NULL,
    expiry_s       INTEGER NOT NULL,
    signal         TEXT NOT NULL,           -- BUY | SELL
    score          REAL,
    strength       REAL,
    agreement      REAL,
    regime         TEXT,
    data_sufficiency REAL,
    entry_price    REAL,
    market_ts      REAL,
    prediction_latency_ms REAL,
    model_version  TEXT,
    context_json   TEXT,                     -- sub-signals etc. (audit)
    result         TEXT,                     -- WIN | LOSS | NULL (pending)
    result_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_pred_created ON predictions(created_at);
CREATE INDEX IF NOT EXISTS idx_pred_asset   ON predictions(asset);
CREATE INDEX IF NOT EXISTS idx_pred_result  ON predictions(result);
"""


class SignalStore:
    def __init__(self, db_path: Path | str = "data/aiaura.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_prediction(
        self,
        *,
        asset: str,
        expiry_s: int,
        signal: str,
        score: float,
        strength: float,
        agreement: float,
        regime: str,
        data_sufficiency: float,
        entry_price: Optional[float],
        market_ts: Optional[float],
        prediction_latency_ms: float,
        model_version: str,
        context: Dict[str, Any],
    ) -> str:
        signal_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO predictions
                   (signal_id, created_at, asset, expiry_s, signal, score, strength,
                    agreement, regime, data_sufficiency, entry_price, market_ts,
                    prediction_latency_ms, model_version, context_json, result, result_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                (signal_id, now, asset, expiry_s, signal, score, strength, agreement,
                 regime, data_sufficiency, entry_price, market_ts,
                 prediction_latency_ms, model_version, json.dumps(context)),
            )
            self._conn.commit()
        return signal_id

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
        """Aggregate performance. Honest: only settled (WIN/LOSS) count toward
        win rate; pending are reported separately."""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
            wins = self._conn.execute("SELECT COUNT(*) FROM predictions WHERE result='WIN'").fetchone()[0]
            losses = self._conn.execute("SELECT COUNT(*) FROM predictions WHERE result='LOSS'").fetchone()[0]
            pending = self._conn.execute("SELECT COUNT(*) FROM predictions WHERE result IS NULL").fetchone()[0]
            by_asset = self._conn.execute(
                """SELECT asset,
                          SUM(result='WIN') AS wins,
                          SUM(result='LOSS') AS losses,
                          SUM(result IS NULL) AS pending
                   FROM predictions GROUP BY asset ORDER BY (wins+losses) DESC"""
            ).fetchall()
            by_expiry = self._conn.execute(
                """SELECT expiry_s,
                          SUM(result='WIN') AS wins,
                          SUM(result='LOSS') AS losses
                   FROM predictions GROUP BY expiry_s ORDER BY expiry_s"""
            ).fetchall()
        settled = wins + losses
        win_rate = (wins / settled) if settled else None
        # max losing streak over settled predictions (chronological)
        streak = self._max_losing_streak()
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "settled": settled,
            "win_rate": win_rate,
            "max_losing_streak": streak,
            "by_asset": [dict(r) for r in by_asset],
            "by_expiry": [dict(r) for r in by_expiry],
            "disclaimer": (
                "Win rate is over user-reported settled signals only; sample may "
                "be tiny and unrepresentative. Baseline heuristic, not a validated "
                "edge. No guarantee of future performance."
            ),
        }

    def _max_losing_streak(self) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result FROM predictions WHERE result IS NOT NULL ORDER BY result_at"
            ).fetchall()
        streak = worst = 0
        for r in rows:
            if r["result"] == "LOSS":
                streak += 1
                worst = max(worst, streak)
            else:
                streak = 0
        return worst
