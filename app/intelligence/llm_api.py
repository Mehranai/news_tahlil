"""Structured completion clients for supported LLM API protocols."""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import ClassificationSettings
from app.intelligence.contracts import ClassificationError


@dataclass(frozen=True)
class Completion:
    content: str
    model_version: str | None
    input_tokens: int | None
    output_tokens: int | None


class ModelAPI(Protocol):
    def complete(self, instructions: str, data: str, schema: dict[str, Any]) -> Completion: ...


class ModelHTTP:
    def __init__(
        self, client: httpx.Client, settings: ClassificationSettings, api_key: str | None
    ) -> None:
        self.client = client
        self.settings = settings
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        url = f"{str(self.settings.base_url).rstrip('/')}/{path}"
        try:
            response = self.client.post(
                url,
                json=data,
                headers=self.headers,
                timeout=self.settings.timeout_seconds,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise ClassificationError("provider_timeout", halt_batch=True) from None
        except httpx.RequestError:
            raise ClassificationError("provider_network_error", halt_batch=True) from None

        if not response.is_success:
            status = response.status_code
            if status in {401, 403}:
                code = "provider_authentication_failed"
            elif status == 429:
                code = "provider_rate_limited"
            elif status >= 500:
                code = "provider_unavailable"
            else:
                code = f"provider_http_{status}"
            raise ClassificationError(code, halt_batch=True)
        try:
            payload = response.json()
        except ValueError:
            raise ClassificationError("invalid_provider_response") from None
        if not isinstance(payload, dict):
            raise ClassificationError("invalid_provider_response")
        return payload


class ChatCompletionsAPI:
    def __init__(self, http: ModelHTTP) -> None:
        self.http = http

    def complete(self, instructions: str, data: str, schema: dict[str, Any]) -> Completion:
        payload = self.http.post(
            "chat/completions",
            {
                "model": self.http.settings.model,
                "messages": _messages(instructions, data),
                "stream": False,
                "max_completion_tokens": self.http.settings.max_output_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "iran_relevance", "strict": True, "schema": schema},
                },
            },
        )
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise ClassificationError("model_refused")
            if choice.get("finish_reason") != "stop":
                raise ClassificationError("model_output_incomplete")
            content = _content(message)
        except (TypeError, KeyError, IndexError, AttributeError):
            raise ClassificationError("invalid_provider_response") from None

        usage = payload.get("usage")
        tokens = usage if isinstance(usage, dict) else {}
        return Completion(
            content,
            _version(payload.get("model")),
            _token_count(tokens.get("prompt_tokens")),
            _token_count(tokens.get("completion_tokens")),
        )


class OllamaAPI:
    def __init__(self, http: ModelHTTP) -> None:
        self.http = http

    def complete(self, instructions: str, data: str, schema: dict[str, Any]) -> Completion:
        payload = self.http.post(
            "api/chat",
            {
                "model": self.http.settings.model,
                "messages": _messages(instructions, data),
                "stream": False,
                "think": False,
                "format": schema,
                "options": {
                    "temperature": 0,
                    "num_predict": self.http.settings.max_output_tokens,
                },
            },
        )
        if payload.get("done") is not True or payload.get("done_reason") == "length":
            raise ClassificationError("model_output_incomplete")
        return Completion(
            _content(payload.get("message")),
            _version(payload.get("model")),
            _token_count(payload.get("prompt_eval_count")),
            _token_count(payload.get("eval_count")),
        )


def _messages(instructions: str, data: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": instructions}, {"role": "user", "content": data}]


def _content(message: object) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ClassificationError("empty_model_output")
    return content


def _version(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value.strip()) <= 160:
        return value.strip()
    return None


def _token_count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2_147_483_647 else None
