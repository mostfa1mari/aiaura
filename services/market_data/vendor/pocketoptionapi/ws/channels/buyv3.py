import time
from pocketoptionapi.ws.channels.base import Base
import logging
import pocketoptionapi.global_value as global_value

logger = logging.getLogger(__name__)


class Buyv3(Base):
    name = "sendMessage"

    def __call__(self, amount, active, direction, duration, request_id):
        is_demo = 1 if self.api.websocket_client.is_demo else 0

        data_dict = {
            "asset": active,
            "amount": amount,
            "action": direction,
            "isDemo": is_demo,
            "requestId": request_id,
            "optionType": 100,
            "time": duration,
        }

        message = ["openOrder", data_dict]
        self.send_websocket_request(self.name, message, str(request_id))


class Buyv3_by_raw_expired(Base):
    name = "sendMessage"

    def __call__(self, price, active, direction, option, expired, request_id):
        if option == "turbo":
            option_id = 3
        elif option == "binary":
            option_id = 1
        else:
            option_id = 3

        data = {
            "body": {
                "price": price,
                "active_id": active,
                "expired": int(expired),
                "direction": direction.lower(),
                "option_type_id": option_id,
                "user_balance_id": int(global_value.balance_id) if global_value.balance_id else 0,
            },
            "name": "binary-options.open-option",
            "version": "1.0",
        }
        self.send_websocket_request(self.name, data, str(request_id))
