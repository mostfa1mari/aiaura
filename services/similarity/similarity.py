"""k-NN historical similarity over standardized feature vectors.

Build once from labelled history (timestamp, features, outcome). ``query``
returns the nearest historical states and their forward directional rate. To
avoid look-ahead, pass ``as_of`` so only states strictly BEFORE that time are
considered — exactly what is knowable when a live signal is generated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class SimilarityResult:
    n_neighbors: int
    similarity_score: float          # mean similarity of the neighbours (0..1)
    directional_rate: float          # fraction of neighbours whose outcome was UP
    neighbor_timestamps: List[float] = field(default_factory=list)
    confident: bool = False          # False when the sample is too small
    note: str = ""

    @property
    def leans(self) -> str:
        return "UP" if self.directional_rate > 0.5 else ("DOWN" if self.directional_rate < 0.5 else "FLAT")


class HistoricalSimilarity:
    def __init__(self, rows: Sequence[Tuple[float, Sequence[float], int]],
                 min_confident_neighbors: int = 20):
        # rows: (timestamp, feature_row, label 1=UP/0=DOWN)
        self._ts = np.array([r[0] for r in rows], dtype=float)
        self._X = np.array([list(r[1]) for r in rows], dtype=float) if rows else np.empty((0, 0))
        self._y = np.array([int(r[2]) for r in rows], dtype=int)
        self._min_confident = min_confident_neighbors
        if self._X.size:
            self._mean = self._X.mean(axis=0)
            self._std = self._X.std(axis=0)
            self._std[self._std == 0] = 1.0
            self._Xz = (self._X - self._mean) / self._std
        else:
            self._mean = self._std = None
            self._Xz = self._X

    def __len__(self) -> int:
        return int(self._ts.shape[0])

    def query(self, features: Sequence[float], k: int = 20,
              as_of: Optional[float] = None) -> SimilarityResult:
        if self._X.size == 0:
            return SimilarityResult(0, 0.0, 0.5, note="no history")
        mask = self._ts < as_of if as_of is not None else np.ones_like(self._ts, dtype=bool)
        idx = np.nonzero(mask)[0]
        if idx.size == 0:
            return SimilarityResult(0, 0.0, 0.5, note="no prior history before as_of")

        q = (np.asarray(features, dtype=float) - self._mean) / self._std
        d = np.linalg.norm(self._Xz[idx] - q, axis=1)
        take = min(k, idx.size)
        nearest_local = np.argsort(d)[:take]
        nearest = idx[nearest_local]
        dists = d[nearest_local]
        sim = 1.0 / (1.0 + dists)                 # 0..1, 1 = identical
        directional = float(self._y[nearest].mean())
        confident = take >= self._min_confident
        note = "" if confident else f"only {take} neighbours (< {self._min_confident}); low confidence"
        return SimilarityResult(
            n_neighbors=int(take),
            similarity_score=float(sim.mean()),
            directional_rate=directional,
            neighbor_timestamps=[float(self._ts[i]) for i in nearest],
            confident=confident,
            note=note,
        )
