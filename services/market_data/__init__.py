"""AI AURA market-data service.

Public surface:
    MarketDataProvider          — abstract provider interface (canonical types)
    PocketOptionMarketDataProvider — Pocket Option OTC implementation
    TickStore                   — raw tick persistence (Parquet)

AI AURA code outside this package must import ONLY from here (never from the
vendored pocketoptionapi library directly).
"""

from services.market_data.provider import (
    SCHEMA_VERSION,
    AssetInfo,
    CanonicalCandle,
    CanonicalTick,
    HealthStatus,
    MarketDataError,
    AssetUnavailableError,
    ProviderConnectionError,
    MarketDataProvider,
)
from services.market_data.storage import TickStore
from services.market_data.quality import (
    QualityReport,
    QualityIssue,
    TickQualityMonitor,
    analyze_ticks,
    summarize_connection_events,
)
from services.market_data.candles import (
    TIMEFRAMES,
    CandleBuilder,
    MultiTimeframeCandleBuilder,
    build_candles,
    bucket_start,
)

__all__ = [
    "SCHEMA_VERSION",
    "AssetInfo",
    "CanonicalCandle",
    "CanonicalTick",
    "HealthStatus",
    "MarketDataError",
    "AssetUnavailableError",
    "ProviderConnectionError",
    "MarketDataProvider",
    "TickStore",
    # quality (Phase 4)
    "QualityReport",
    "QualityIssue",
    "TickQualityMonitor",
    "analyze_ticks",
    "summarize_connection_events",
    # candles (Phase 5)
    "TIMEFRAMES",
    "CandleBuilder",
    "MultiTimeframeCandleBuilder",
    "build_candles",
    "bucket_start",
]
