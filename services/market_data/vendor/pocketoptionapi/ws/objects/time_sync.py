import logging
import time
from collections import deque
from datetime import datetime, timezone
from tzlocal import get_localzone

logger = logging.getLogger(__name__)

_SAMPLE_WINDOW = 20


class TimeSynchronizer:
    """Synchronizes local clock with PocketOption server time.

    The HTTP Date header from the WebSocket upgrade provides an initial UTC
    reference.  Stream timestamps (updateStream) may carry a timezone offset
    (observed: UTC+2) which is auto-detected on the first tick.  Internally
    the synchronizer stores everything in UTC; the server-native offset is
    tracked separately and exposed via ``get_server_native_time()`` for use
    in API requests (loadHistoryPeriod, etc.) that expect the server's native
    timestamps.
    """

    def __init__(self):
        self.server_time_reference: float | None = None
        self.local_time_reference: float | None = None
        self.offset: float | None = None
        self._samples: deque = deque(maxlen=_SAMPLE_WINDOW)
        self._stream_tz_offset: float = 0.0
        self._tz_detection_done: bool = False

    @property
    def server_timestamps(self):
        return self.server_time_reference

    @server_timestamps.setter
    def server_timestamps(self, value):
        self.server_time_reference = value

    @property
    def server_timestamp(self):
        return self.server_time_reference

    def synchronize(self, server_time) -> None:
        """Called with each server timestamp to update the offset.

        The first call is typically from the HTTP Date header (UTC).  When the
        first stream tick arrives with a different epoch (UTC+N), the offset
        between the two is detected automatically and future stream timestamps
        are normalised to UTC before being fed into the averaging filter.
        """
        try:
            local_now = time.time()
            srv = float(server_time)

            if not self._tz_detection_done and self.offset is not None:
                expected_utc = local_now - self.offset
                diff = srv - expected_utc
                if abs(diff) > 1800:
                    self._stream_tz_offset = round(diff / 3600) * 3600
                    logger.info(f"Detected stream timezone offset: "
                                f"{self._stream_tz_offset:.0f}s "
                                f"({self._stream_tz_offset / 3600:+.0f}h from UTC)")
                self._tz_detection_done = True

            if self._stream_tz_offset:
                srv -= self._stream_tz_offset

            sample_offset = local_now - srv
            self._samples.append(sample_offset)

            if len(self._samples) >= 3:
                sorted_samples = sorted(self._samples)
                trimmed = sorted_samples[1:-1] if len(sorted_samples) > 4 else sorted_samples
                self.offset = sum(trimmed) / len(trimmed)
            else:
                self.offset = sample_offset

            self.server_time_reference = srv
            self.local_time_reference = local_now

            logger.debug(f"Time synced — server(UTC): {srv:.3f}, offset: {self.offset:.4f}s, "
                         f"samples: {len(self._samples)}")
        except Exception as e:
            logger.error(f"synchronize error: {e}")

    def get_synced_time(self) -> float:
        """Returns the current estimated server time in UTC."""
        if not self.is_synced() or self.offset is None:
            logger.warning("Time not synced, returning local time")
            return time.time()
        try:
            return time.time() - self.offset
        except Exception as e:
            logger.error(f"get_synced_time error: {e}")
            return time.time()

    def get_server_native_time(self) -> float:
        """Returns the current server time in the server's native timezone.

        Use this for API requests (loadHistoryPeriod etc.) that expect timestamps
        in the server's timezone (typically UTC+2).
        """
        return self.get_synced_time() + self._stream_tz_offset

    def get_synced_datetime(self) -> datetime:
        """Returns the current UTC server time as a timezone-aware local datetime."""
        try:
            synced = self.get_synced_time()
            local_tz = get_localzone()
            return datetime.fromtimestamp(synced, tz=local_tz)
        except Exception as e:
            logger.error(f"get_synced_datetime error: {e}")
            return datetime.now(get_localzone())

    def is_synced(self) -> bool:
        return (self.server_time_reference is not None
                and self.local_time_reference is not None
                and self.offset is not None)

    def update_sync(self, new_server_time) -> None:
        self.synchronize(new_server_time)
