"""Module for Pocket Option candles websocket channel."""
import logging
import time

from pocketoptionapi.ws.channels.base import Base

logger = logging.getLogger(__name__)


def _make_index() -> int:
    """Generates a unique request index matching the web client format."""
    return int(time.time() * 100)


class GetCandles(Base):
    name = "getCandles"

    def __call__(self, active_id, interval, count, end_time):
        """Request historical candles.

        :param active_id: Asset symbol (e.g. "EURUSD_otc").
        :param interval: Candle period in seconds.
        :param count: Offset / number of candles.
        :param end_time: Unix timestamp for the end of the range.
        """
        data = {
            "asset": str(active_id),
            "index": _make_index(),
            "offset": count,
            "period": interval,
            "time": end_time,
        }

        data = ["loadHistoryPeriod", data]
        logger.info(f"GetCandles > {data}")

        self.send_websocket_request(self.name, data)
