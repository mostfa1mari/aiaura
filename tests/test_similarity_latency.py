"""Historical similarity + latency viability tests (offline)."""

from services.latency import assess, summarize
from services.similarity import HistoricalSimilarity

BASE = 1787000000


def test_similarity_finds_like_states_and_direction():
    # two clusters: feature ~[0,..] -> UP(1), feature ~[10,..] -> DOWN(0)
    rows = []
    for i in range(100):
        rows.append((BASE + i, [0.0 + (i % 3) * 0.01, 1.0], 1))
    for i in range(100):
        rows.append((BASE + 1000 + i, [10.0 + (i % 3) * 0.01, 1.0], 0))
    sim = HistoricalSimilarity(rows, min_confident_neighbors=20)
    up = sim.query([0.0, 1.0], k=20)
    assert up.directional_rate > 0.8 and up.leans == "UP" and up.confident
    down = sim.query([10.0, 1.0], k=20)
    assert down.directional_rate < 0.2 and down.leans == "DOWN"


def test_similarity_no_lookahead_as_of():
    rows = [(BASE + i, [float(i), 1.0], 1 if i < 50 else 0) for i in range(100)]
    sim = HistoricalSimilarity(rows)
    # as_of excludes rows at/after the cutoff -> only early (UP) states available
    r = sim.query([10.0, 1.0], k=10, as_of=BASE + 50)
    assert all(ts < BASE + 50 for ts in r.neighbor_timestamps)


def test_similarity_low_confidence_small_sample():
    rows = [(BASE + i, [float(i), 1.0], 1) for i in range(5)]
    r = HistoricalSimilarity(rows, min_confident_neighbors=20).query([2.0, 1.0], k=20)
    assert r.n_neighbors == 5 and r.confident is False and "low confidence" in r.note


def test_similarity_empty():
    r = HistoricalSimilarity([]).query([1.0, 2.0])
    assert r.n_neighbors == 0 and r.directional_rate == 0.5


def test_latency_assess_verdicts():
    # 60s horizon, ~1.6s total -> viable
    assert assess(80, 60, tick_age_ms=50).verdict == "viable"
    # 5s horizon, ~1.6s total -> marginal/not_viable
    v5 = assess(80, 5, tick_age_ms=50)
    assert v5.verdict in ("marginal", "not_viable")
    # 3s horizon with 1.5s exec -> heavy fraction
    v3 = assess(100, 3, tick_age_ms=100)
    assert v3.fraction_of_horizon > 0.4 and v3.verdict == "not_viable"


def test_latency_summarize_uses_p95():
    s = summarize([80] * 100, horizon_s=5, tick_ages_ms=[50] * 100)
    assert s["n"] == 100 and s["verdict"] in ("marginal", "not_viable")
    s60 = summarize([80] * 100, horizon_s=60, tick_ages_ms=[50] * 100)
    assert s60["verdict"] == "viable"
