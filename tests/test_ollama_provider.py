from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from aiq import ModelMessage, ModelRequest, ToolDefinition
from aiq.providers import OllamaProvider


class OllamaProviderTests(unittest.TestCase):
    def test_model_only_constructor_owns_a_closeable_client(self) -> None:
        provider = OllamaProvider(model="llama", timeout=1)
        asyncio.run(provider.aclose())

    def test_maps_one_http_request_and_parses_tool_call(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "content": "Checking",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "weather",
                                    "arguments": {"city": "Tbilisi"},
                                },
                            }
                        ],
                    },
                    "prompt_eval_count": 10,
                    "eval_count": 3,
                },
            )

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                provider = OllamaProvider(client, model="llama")
                return await provider.complete(
                    ModelRequest(
                        (ModelMessage("user", "weather"),),
                        (
                            ToolDefinition(
                                "weather",
                                "Weather",
                                {"type": "object", "properties": {}},
                            ),
                        ),
                    ),
                    operation_id="stable-operation",
                )

        response = asyncio.run(scenario())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].headers["Idempotency-Key"], "stable-operation")
        self.assertEqual(response.tool_calls[0].name, "weather")
        self.assertEqual(response.usage.input_tokens, 10)

    def test_think_option_is_omitted_or_forwarded_exactly(self) -> None:
        async def scenario(think: bool | None) -> dict[str, object]:
            seen: list[dict[str, object]] = []

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(json.loads(request.content))
                return httpx.Response(
                    200,
                    json={"message": {"role": "assistant", "content": "done"}},
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                provider = OllamaProvider(client, model="qwen3:4b", think=think)
                await provider.complete(
                    ModelRequest((ModelMessage("user", "work"),)),
                    operation_id="stable-operation",
                )
            return seen[0]

        for think in (None, True, False):
            with self.subTest(think=think):
                payload = asyncio.run(scenario(think))
                if think is None:
                    self.assertNotIn("think", payload)
                else:
                    self.assertIs(payload["think"], think)


if __name__ == "__main__":
    unittest.main()
