from __future__ import annotations

import unittest

from aiq import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolDefinition,
)


class ModelValueTests(unittest.TestCase):
    def test_request_round_trips_through_plain_event_data(self) -> None:
        request = ModelRequest(
            messages=(ModelMessage("user", "weather"),),
            tools=(
                ToolDefinition(
                    "weather",
                    "Current weather",
                    {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                ),
            ),
            model="test",
        )

        self.assertEqual(ModelRequest.from_data(request.to_data()), request)

    def test_response_supports_text_and_tool_calls_without_policy_decision(self) -> None:
        response = ModelResponse(
            message=ModelMessage("assistant", "I'll check."),
            tool_calls=(ToolCall("call-1", "weather", {"city": "Tbilisi"}),),
            usage=ModelUsage(12, 4),
            provider_request_id="provider-1",
        )

        self.assertEqual(ModelResponse.from_data(response.to_data()), response)

    def test_values_reject_non_json_and_duplicate_identifiers(self) -> None:
        with self.assertRaises(TypeError):
            ToolCall("call-1", "weather", {"bad": object()})
        with self.assertRaises(ValueError):
            ModelResponse(
                ModelMessage("assistant", ""),
                (
                    ToolCall("same", "a", {}),
                    ToolCall("same", "b", {}),
                ),
            )


if __name__ == "__main__":
    unittest.main()
