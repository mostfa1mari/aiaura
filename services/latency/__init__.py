"""Latency viability (Phase 20).

For very short expiries the round-trip (tick age + prediction + user execution)
can consume a large fraction of the horizon, which erodes any edge. This module
turns measured latencies into an honest viability verdict — never asserting a
short horizon is usable without the numbers.
"""

from services.latency.analysis import LatencyVerdict, assess, summarize

__all__ = ["LatencyVerdict", "assess", "summarize"]
