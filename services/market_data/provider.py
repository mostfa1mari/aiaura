"""Canonical market-data model and provider abstraction.

Everything downstream of this module (storage, candle engine, features,
strategies, ML) depends only on these types — never on a concrete provider.
Swapping the data source later means writing one new MarketDataProvider
subclass, nothing else.

Timestamps: all canonical timestamps are Unix epoch seconds in UTC.
Provider-native timestamps (e.g. Pocket Option's UTC+2 wire clock) are
normalized at the provider boundary; the raw wire value is preserved in
``CanonicalTick.raw_source_timestamp`` for auditability.
"""

from __future__ import annotations

import abc
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1.0.0"


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------

class MarketDataError(Exception):
    """Base error for the market-data layer."""


class ProviderConnectionError(MarketDataError):
    """Could not establish or keep a provider connection."""


class AssetUnavailableError(MarketDataError):
    """Requested asset does not exist or is not currently tradeable/streamable."""


# ----------------------------------------------------------------------
# Canonical types
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalTick:
    """One market tick, normalized.

    latency_ms is the tick's age at receive time measured against the
    provider's synced server-clock estimate. It is a *relative* estimate
    (the clock offset itself is estimated from the same stream), can be
    slightly negative, and must not be read as absolute one-way latency.
    """

    tick_id: str
    asset: str
    price: float
    source_timestamp: float        # UTC epoch seconds (normalized)
    received_timestamp: float      # UTC epoch seconds (local wall clock)
    latency_ms: float
    provider: str
    schema_version: str = SCHEMA_VERSION
    raw_source_timestamp: float = 0.0  # as received on the wire (server-native)


@dataclass(frozen=True)
class CanonicalCandle:
    """One OHLC candle. ``complete=False`` marks a still-forming candle."""

    asset: str
    timeframe_s: int
    timestamp: float               # candle open time, UTC epoch seconds
    open: float
    high: float
    low: float
    close: float
    tick_count: Optional[int] = None
    volume: Optional[float] = None
    complete: bool = True
    provider: str = ""
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class AssetInfo:
    """One entry of the provider's asset catalog."""

    symbol: str
    name: str
    category: str
    payout: Optional[float]
    is_available: bool
    timeframes: Tuple[int, ...] = ()

    @property
    def is_otc(self) -> bool:
        return self.symbol.endswith("_otc")


@dataclass
class HealthStatus:
    """Snapshot of provider health for monitors and dashboards."""

    connected: bool
    time_synced: bool
    subscribed_assets: Tuple[str, ...]
    ticks_received: int
    last_tick_at: Optional[float]        # received_timestamp of newest tick
    last_tick_age_s: Optional[float]
    reconnect_count: int
    connection_events: List[dict] = field(default_factory=list)
    detail: str = ""

    @property
    def status(self) -> str:
        """DOWN | DEGRADED | GOOD (coarse; monitors may refine)."""
        if not self.connected:
            return "DOWN"
        if not self.time_synced:
            return "DEGRADED"
        if self.subscribed_assets and (
            self.last_tick_age_s is None or self.last_tick_age_s > 10.0
        ):
            return "DEGRADED"
        return "GOOD"


TickListener = Callable[[CanonicalTick], None]


# ----------------------------------------------------------------------
# Provider interface
# ----------------------------------------------------------------------

class MarketDataProvider(abc.ABC):
    """Read-only market-data interface.

    Deliberately exposes NO order/trade/account-mutation methods, and no
    subclass in AI AURA may add any. This boundary is enforced by
    tests/test_no_order_execution.py.
    """

    name: str = "abstract"

    def __init__(self) -> None:
        self._listeners: List[TickListener] = []
        self._listener_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------

    @abc.abstractmethod
    def connect(self) -> None:
        """Establish the connection. Raises ProviderConnectionError on failure."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Tear down the connection and background workers."""

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    # -- catalog -------------------------------------------------------

    @abc.abstractmethod
    def get_assets(self) -> Dict[str, AssetInfo]:
        """Full asset catalog as reported live by the provider."""

    # -- streaming -----------------------------------------------------

    @abc.abstractmethod
    def subscribe(self, asset: str) -> None:
        """Start the real-time tick stream for an asset.

        Raises AssetUnavailableError if the asset is unknown or unavailable.
        """

    @abc.abstractmethod
    def unsubscribe(self, asset: str) -> None: ...

    @abc.abstractmethod
    def get_latest_tick(self, asset: str) -> Optional[CanonicalTick]: ...

    @abc.abstractmethod
    def get_realtime_ticks(self, asset: str, limit: int = 100) -> List[CanonicalTick]: ...

    # -- history -------------------------------------------------------

    @abc.abstractmethod
    def get_historical_candles(
        self,
        asset: str,
        timeframe_s: int,
        end_time: Optional[float] = None,
        pages: int = 1,
    ) -> List[CanonicalCandle]:
        """Historical OHLC, oldest first. Empty list when nothing was returned."""

    # -- health --------------------------------------------------------

    @abc.abstractmethod
    def health_check(self) -> HealthStatus: ...

    # -- tick listeners (concrete) ------------------------------------

    def add_tick_listener(self, listener: TickListener) -> None:
        with self._listener_lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_tick_listener(self, listener: TickListener) -> None:
        with self._listener_lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit_tick(self, tick: CanonicalTick) -> None:
        with self._listener_lock:
            listeners: Sequence[TickListener] = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(tick)
            except Exception:  # a bad listener must never kill the feed
                import logging
                logging.getLogger(__name__).exception("tick listener failed")
