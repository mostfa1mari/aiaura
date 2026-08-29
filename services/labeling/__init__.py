"""Prediction-target / labeling (Phase 6).

Defines WHAT AI AURA predicts (directional movement over an expiry) and HOW a
historical outcome is measured (reference-price methodology), with strict
no-look-ahead guarantees. Labels are a research/backtest/training concern only
— they are NEVER available to the feature engine at inference time.
"""

from services.labeling.target import (
    Direction,
    Label,
    LabelConfig,
    ReferenceSeries,
    generate_labels,
    infer_price_precision,
    make_label,
)

__all__ = [
    "Direction",
    "Label",
    "LabelConfig",
    "ReferenceSeries",
    "generate_labels",
    "infer_price_precision",
    "make_label",
]
