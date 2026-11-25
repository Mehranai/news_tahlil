"""Send one plain-text message through the Telegram Bot API."""

import re

import httpx


class DeliveryError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retry_after: int | None = None,
        delivery_unknown: bool = False,
        halt_batch: bool = False,
    ) -> None:
        self.code = code
        self.retry_after = retry_after
        self.delivery_unknown = delivery_unknown
        self.halt_batch = halt_batch
        super().__init__(code)


class TelegramSender:
    def __init__(
        self, client: httpx.Client, token: str, chat_id: str, *, timeout_seconds: float
    ) -> None:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Invalid TELEGRAM_BOT_TOKEN format")
        if re.fullmatch(r"-?[0-9]{1,19}", chat_id) and -(2**63) <= int(chat_id) < 2**63:
            self._chat_id: int | str = int(chat_id)
        elif re.fullmatch(r"@[A-Za-z0-9_]{1,64}", chat_id):
            self._chat_id = chat_id
        else:
            raise ValueError("TELEGRAM_CHAT_ID must be a numeric ID or @username")
        self._client = client
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._timeout_seconds = timeout_seconds

    def send(self, text: str) -> str:
        try:
            response = self._client.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise DeliveryError(
                "telegram_connection_failed", retry_after=0, halt_batch=True
            ) from None
        except httpx.RequestError:
            raise DeliveryError(
                "delivery_unknown", delivery_unknown=True, halt_batch=True
            ) from None

        try:
            payload = response.json()
        except ValueError:
            payload = None
        data = payload if isinstance(payload, dict) else {}
        code = data.get("error_code") if data.get("ok") is False else None
        status = code if type(code) is int else response.status_code
        if response.status_code == 429 or status == 429:
            parameters = data.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if type(retry_after) is not int or retry_after <= 0:
                header = response.headers.get("retry-after", "")
                retry_after = int(header) if re.fullmatch(r"[0-9]{1,9}", header) else 30
            raise DeliveryError(
                "telegram_rate_limited",
                retry_after=min(max(retry_after, 1), 2**31 - 1),
                halt_batch=True,
            )
        if response.is_success and data.get("ok") is True:
            result = data.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if type(message_id) is int and message_id >= 0:
                return str(message_id)
        if type(code) is int and 500 <= code < 600:
            raise DeliveryError("telegram_unavailable", retry_after=30, halt_batch=True)
        if 400 <= status < 500:
            reason = (
                "telegram_authentication_failed" if status in {401, 403} else f"telegram_{status}"
            )
            raise DeliveryError(reason, halt_batch=status in {400, 401, 403, 404})
        raise DeliveryError("delivery_unknown", delivery_unknown=True, halt_batch=True)
