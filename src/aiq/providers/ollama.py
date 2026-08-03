"""One-call, non-streaming Ollama adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

try:
    import httpx
except ImportError as error:  # pragma: no cover - depends on optional installation
    raise ImportError(
        "OllamaProvider requires the 'ollama' extra: pip install aiq[ollama]"
    ) from error

from ..models import (
    ModelCallFailedError,
    ModelCallRejectedError,
    ModelMessage,
    ModelOutputRejectedError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


class OllamaProvider:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        model: str,
        url: str = "http://127.0.0.1:11434/api/chat",
        timeout: float = 120.0,
        think: bool | None = None,
    ) -> None:
        if not model:
            raise ValueError("Ollama model must not be empty")
        if timeout <= 0:
            raise ValueError("Ollama timeout must be positive")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._model = model
        self._url = url
        self._think = think

    async def aclose(self) -> None:
        """Close the internally-created HTTP client, if this provider owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        request: ModelRequest,
        *,
        operation_id: str,
    ) -> ModelResponse:
        if request.artifacts:
            raise ModelCallRejectedError(
                "OllamaProvider does not resolve AIQ artifact references"
            )
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": (
                (
                    [{"role": "system", "content": request.instruction.text}]
                    if request.instruction is not None
                    else []
                )
                + [self._message_payload(message) for message in request.messages]
            ),
            "stream": False,
        }
        if self._think is not None:
            payload["think"] = self._think
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": _plain_json(tool.input_schema),
                    },
                }
                for tool in request.tools
            ]
        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers={"Idempotency-Key": operation_id},
            )
            response.raise_for_status()
            data = response.json()
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as error:
            raise ModelCallFailedError(f"Ollama request failed: {error}") from error
        except ValueError as error:
            raise ModelOutputRejectedError("Ollama returned invalid JSON") from error
        return self._parse_response(data)

    @staticmethod
    def _message_payload(message: ModelMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.name is not None:
            payload["name"] = message.name
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @staticmethod
    def _parse_response(data: Any) -> ModelResponse:
        if not isinstance(data, Mapping) or not isinstance(
            data.get("message"), Mapping
        ):
            raise ModelOutputRejectedError("Ollama response is missing message")
        message_data = data["message"]
        role = message_data.get("role", "assistant")
        content = message_data.get("content", "")
        if not isinstance(role, str) or not isinstance(content, str):
            raise ModelOutputRejectedError("Ollama message has invalid role or content")
        raw_calls = message_data.get("tool_calls", ())
        if not isinstance(raw_calls, (list, tuple)):
            raise ModelOutputRejectedError("Ollama tool_calls must be an array")
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, Mapping):
                raise ModelOutputRejectedError("Ollama tool call must be an object")
            function = raw_call.get("function")
            if not isinstance(function, Mapping) or not isinstance(
                function.get("name"), str
            ):
                raise ModelOutputRejectedError(
                    "Ollama tool call is missing function name"
                )
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError as error:
                    raise ModelOutputRejectedError(
                        "Ollama tool arguments are invalid JSON"
                    ) from error
            if not isinstance(arguments, Mapping):
                raise ModelOutputRejectedError(
                    "Ollama tool arguments must be an object"
                )
            call_id = raw_call.get("id") or f"ollama-call-{index + 1}"
            calls.append(ToolCall(str(call_id), str(function["name"]), arguments))
        usage = ModelUsage(
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
        )
        return ModelResponse(
            message=ModelMessage(role, content),
            tool_calls=tuple(calls),
            usage=usage,
            provider_request_id=(
                str(data["created_at"]) if data.get("created_at") else None
            ),
        )
