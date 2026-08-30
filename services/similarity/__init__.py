"""Historical similarity (Phase 13).

Finds past market states similar to the current one (by standardized feature
distance) and reports what happened next. Internal/advisory only — it returns
the neighbour count and a confidence caveat so a tiny sample is never mistaken
for proof.
"""

from services.similarity.similarity import HistoricalSimilarity, SimilarityResult

__all__ = ["HistoricalSimilarity", "SimilarityResult"]
