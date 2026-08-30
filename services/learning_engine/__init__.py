"""Learning engine (Phases 10, 14, 16).

Builds no-look-ahead datasets from candles, trains/evaluates ML models with
chronological walk-forward validation, versions them in a registry with a
champion/challenger discipline, and runs controlled self-learning from
accumulated data + WIN/LOSS feedback.

Nothing here fabricates performance: a challenger is promoted only when it beats
the champion on unseen data by a significant margin.
"""

from services.learning_engine.dataset import DatasetRow, build_dataset
from services.learning_engine.registry import ModelRecord, ModelRegistry

__all__ = ["DatasetRow", "build_dataset", "ModelRecord", "ModelRegistry"]
