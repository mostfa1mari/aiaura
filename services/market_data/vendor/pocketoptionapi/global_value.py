# Minimal global state — only values that MUST be shared across threads.
# Per-instance state (assets, deals, streams) lives in PocketOptionAPI.

websocket_is_connected = False

ssl_Mutual_exclusion = False
ssl_Mutual_exclusion_write = False

SSID = None

check_websocket_if_error = False
websocket_error_reason = None

balance_id = None
balance = None
balance_type = None
balance_updated = None

result = None
order_data = {}
account_id = None
