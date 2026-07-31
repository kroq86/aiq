from __future__ import annotations

import asyncio
import unittest

from agentlog import (
    FunctionTool,
    ModelCallFailedError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
)
from agentlog.llm import (
    MODEL_CALL_FAILED,
    MODEL_CALL_SUCCEEDED,
    MODEL_OUTPUT_REJECTED,
    TOOL_CALL_SUCCEEDED,
    execute_model_call,
    execute_tool_call,
    read_model_response,
    request_model_call,
    request_tool_call,
    single_tool_call,
    tool_result_message,
)


class _Provider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def complete(self, request, *, operation_id):
        self.calls.append((request, operation_id))
        if self.error:
            raise self.error
        return self.response


class LifecycleTests(unittest.TestCase):
    def test_one_model_and_one_tool_call_have_stable_operation_boundaries(self) -> None:
        request = ModelRequest((ModelMessage("user", "weather"),))
        response = ModelResponse(
            ModelMessage("assistant", "Checking"),
            (ToolCall("call-1", "weather", {"city": "Tbilisi"}),),
        )
        provider = _Provider(response)
        requested = request_model_call(request, model_step=1, causation_id="user-event")

        observations = asyncio.run(execute_model_call(requested, provider))
        self.assertEqual(observations[0].event_type, MODEL_CALL_SUCCEEDED)
        self.assertEqual(provider.calls, [(request, str(requested.event_id))])
        call = single_tool_call(read_model_response(observations[0]))
        assert call is not None

        registry = ToolRegistry()
        registry.register(
            FunctionTool(
                ToolDefinition(
                    "weather",
                    "Weather",
                    {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                ),
                lambda city: {"city": city, "temperature": 23},
            )
        )
        tool_requested = request_tool_call(call, causation_id=str(observations[0].event_id))
        tool_observations = asyncio.run(execute_tool_call(tool_requested, registry))
        self.assertEqual(tool_observations[0].event_type, TOOL_CALL_SUCCEEDED)
        self.assertEqual(
            tool_result_message(tool_observations[0]).content,
            '{"city":"Tbilisi","temperature":23}',
        )

    def test_expected_provider_failure_and_unsupported_multi_tool_output_are_distinct(self) -> None:
        requested = request_model_call(
            ModelRequest((ModelMessage("user", "x"),)),
            model_step=1,
            causation_id="cause",
        )
        failed = asyncio.run(
            execute_model_call(requested, _Provider(error=ModelCallFailedError("timeout")))
        )
        self.assertEqual(failed[0].event_type, MODEL_CALL_FAILED)

        invalid_provider = _Provider(object())
        rejected = asyncio.run(execute_model_call(requested, invalid_provider))
        self.assertEqual(rejected[0].event_type, MODEL_OUTPUT_REJECTED)

        with self.assertRaisesRegex(ValueError, "multiple tool calls"):
            single_tool_call(
                ModelResponse(
                    ModelMessage("assistant", ""),
                    (ToolCall("a", "a", {}), ToolCall("b", "b", {})),
                )
            )


if __name__ == "__main__":
    unittest.main()
