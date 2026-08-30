"""Versioned model registry with champion/challenger (Phase 16).

Each saved model has a metadata record (version, feature_version,
training/validation periods, metrics, timestamp). Exactly one model is the
champion (the one the app uses). A challenger is promoted to champion only by
an explicit call after it beats the incumbent on unseen data (see
self_learning). Rollback = promote an older version. Nothing is deleted.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ModelRecord:
    version: str
    feature_version: str
    model_kind: str
    created_at: float
    train_period: List[float]        # [start_ts, end_ts]
    n_train: int
    n_test: int
    metrics: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def artifact(self) -> str:
        return f"{self.version}.joblib"


class ModelRegistry:
    def __init__(self, root: Path | str = "models"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {"champion": None, "records": {}}

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2), encoding="utf-8")

    # -- saving / loading ---------------------------------------------

    def save(self, model: Any, record: ModelRecord, make_champion: bool = False) -> ModelRecord:
        import joblib

        joblib.dump(model, self.root / record.artifact)
        self._index["records"][record.version] = asdict(record)
        # Only deploy when explicitly told to. A model is NEVER auto-promoted
        # just for being first — an un-vetted challenger must not become the
        # champion the app serves.
        if make_champion:
            self._index["champion"] = record.version
        self._save_index()
        return record

    def load(self, version: str) -> Any:
        import joblib

        path = self.root / f"{version}.joblib"
        if not path.exists():
            raise FileNotFoundError(f"no model artifact for {version}")
        return joblib.load(path)

    # -- champion / challenger ----------------------------------------

    @property
    def champion_version(self) -> Optional[str]:
        return self._index.get("champion")

    def champion_record(self) -> Optional[ModelRecord]:
        v = self.champion_version
        if not v:
            return None
        return ModelRecord(**self._index["records"][v])

    def load_champion(self) -> Optional[Any]:
        v = self.champion_version
        return self.load(v) if v else None

    def promote(self, version: str) -> None:
        if version not in self._index["records"]:
            raise KeyError(f"unknown model version {version}")
        self._index["champion"] = version
        self._save_index()

    def records(self) -> List[ModelRecord]:
        return [ModelRecord(**r) for r in self._index["records"].values()]

    @staticmethod
    def new_version(prefix: str = "m") -> str:
        # time is passed in by callers where determinism matters; here we build a
        # readable-ish unique id without wall-clock coupling in tests.
        return f"{prefix}-{uuid.uuid4().hex[:10]}"
