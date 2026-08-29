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
]
