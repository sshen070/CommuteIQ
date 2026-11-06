import requests

# Telegram Bot API (https://core.telegram.org/bots/api), free. The bot's token is part of
# every request URL, so errors are reported without it: never log a request URL or raw
# requests exception text.

api_url: str = "https://api.telegram.org/bot{token}/{method}"

# Seconds before giving up on a request that isn't a long poll
timeout: int = 10


class TelegramError(RuntimeError):
    def __init__(self, method: str, code: int, description: str):
        super().__init__(f"Telegram {method} failed: HTTP {code} {description}")
        self.code = code


# One Bot API call, returning its result. wait_s must outlast a long poll's own timeout
def call(token: str, method: str, wait_s: float = timeout, **params):
    params = {key: value for key, value in params.items() if value is not None}
    try:
        response = requests.post(api_url.format(token=token, method=method), json=params, timeout=wait_s)
    except requests.RequestException as err:
        raise RuntimeError(f"Telegram {method} failed: {type(err).__name__}") from None

    try:
        body = response.json()
    except ValueError:
        raise TelegramError(method, response.status_code, "(answer wasn't JSON)") from None

    if not body.get("ok"):
        raise TelegramError(method, response.status_code, " ".join(str(body.get("description")).split())[:200])
    return body["result"]
