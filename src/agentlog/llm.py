"""Durable lifecycle helpers for one model call or one tool call.

These functions deliberately stop at a single external-operation boundary.
Applications keep ownership of domain state and reactions; no hidden
reason-act loop or mutable session is introduced here.
"""

from __future__ import annotations

from collections.abc import Mapping

from .core import Event, JsonValue
from .models import (
    ModelCallFailedError,
    ModelCallRejectedError,
    ModelMessage,
    ModelOutputRejectedError,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from .runtime import effect_request
from .tools import ToolArgumentsRejected, ToolExecutionFailed, ToolRegistry

MODEL_CALL_REQUESTED = "ModelCallRequested"
MODEL_CALL_REJECTED = "ModelCallRejected"
MODEL_CALL_SUCCEEDED = "ModelCallSucceeded"
MODEL_CALL_FAILED = "ModelCallFailed"
MODEL_OUTPUT_REJECTED = "ModelOutputRejected"
TOOL_CALL_REQUESTED = "ToolCallRequested"
TOOL_CALL_REJECTED = "ToolCallRejected"
TOOL_CALL_SUCCEEDED = "ToolCallSucceeded"
TOOL_CALL_FAILED = "ToolCallFailed"


def _plain_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def request_model_call(
    request: ModelRequest,
    *,
    model_step: int,
    causation_id: str,
) -> Event:
    if model_step <= 0:
        raise ValueError("model_step must be positive")
    return effect_request(
        MODEL_CALL_REQUESTED,
        {"model_step": model_step, "request": request.to_data()},
        {"causation_id": causation_id},
    )


async def execute_model_call(event: Event, provider: ModelProvider) -> tuple[Event, ...]:
    """Execute exactly one provider request and return one observation."""
    if event.event_type != MODEL_CALL_REQUESTED:
        raise TypeError(f"expected {MODEL_CALL_REQUESTED}, got {event.event_type}")
    request_data = event.data.get("request")
    if not isinstance(request_data, Mapping):
        return (Event(MODEL_CALL_REJECTED, {"reason": "invalid model request"}),)
    try:
        request = ModelRequest.from_data(request_data)
        response = await provider.complete(request, operation_id=str(event.event_id))
        if not isinstance(response, ModelResponse):
            raise ModelOutputRejectedError("provider did not return ModelResponse")
    except ModelCallRejectedError as error:
        return (Event(MODEL_CALL_REJECTED, {"reason": str(error)}),)
    except ModelCallFailedError as error:
        return (Event(MODEL_CALL_FAILED, {"reason": str(error)}),)
    except (ModelOutputRejectedError, TypeError, ValueError) as error:
        return (Event(MODEL_OUTPUT_REJECTED, {"reason": str(error)}),)
    return (
        Event(
            MODEL_CALL_SUCCEEDED,
            {
                "model_step": event.data["model_step"],
                "response": response.to_data(),
            },
        ),
    )


def read_model_response(event: Event) -> ModelResponse:
    if event.event_type != MODEL_CALL_SUCCEEDED:
        raise TypeError(f"expected {MODEL_CALL_SUCCEEDED}, got {event.event_type}")
    response = event.data.get("response")
    if not isinstance(response, Mapping):
        raise ModelOutputRejectedError("model response payload must be an object")
    return ModelResponse.from_data(response)


def single_tool_call(response: ModelResponse) -> ToolCall | None:
    """Apply the 0.2 policy: final text or exactly one tool call."""
    if len(response.tool_calls) > 1:
        raise ModelOutputRejectedError(
            "single-tool policy does not support multiple tool calls"
        )
    return response.tool_calls[0] if response.tool_calls else None


def request_tool_call(call: ToolCall, *, causation_id: str) -> Event:
    return effect_request(
        TOOL_CALL_REQUESTED,
        {"tool_call": call.to_data()},
        {"causation_id": causation_id},
    )


async def execute_tool_call(event: Event, registry: ToolRegistry) -> tuple[Event, ...]:
    """Resolve and execute exactly one registered tool."""
    if event.event_type != TOOL_CALL_REQUESTED:
        raise TypeError(f"expected {TOOL_CALL_REQUESTED}, got {event.event_type}")
    call_data = event.data.get("tool_call")
    if not isinstance(call_data, Mapping):
        return (Event(TOOL_CALL_REJECTED, {"reason": "invalid tool call"}),)
    try:
        call = ToolCall.from_data(call_data)
        tool = registry.validate(call.name, call.arguments)
    except (LookupError, ToolArgumentsRejected, TypeError, ValueError) as error:
        return (
            Event(
                TOOL_CALL_REJECTED,
                {
                    "call_id": str(call_data.get("call_id", "")),
                    "reason": str(error),
                },
            ),
        )
    try:
        result = await tool.execute(call.arguments, operation_id=str(event.event_id))
    except ToolArgumentsRejected as error:
        return (
            Event(TOOL_CALL_REJECTED, {"call_id": call.call_id, "reason": str(error)}),
        )
    except ToolExecutionFailed as error:
        return (
            Event(TOOL_CALL_FAILED, {"call_id": call.call_id, "reason": str(error)}),
        )
    return (
        Event(
            TOOL_CALL_SUCCEEDED,
            {"call_id": call.call_id, "name": call.name, "result": result},
        ),
    )


def tool_result_message(event: Event) -> ModelMessage:
    if event.event_type != TOOL_CALL_SUCCEEDED:
        raise TypeError(f"expected {TOOL_CALL_SUCCEEDED}, got {event.event_type}")
    # ModelMessage content is text across providers.  JSON-compatible values
    # use a deterministic compact serialization at this boundary.
    import json

    result: JsonValue = event.data["result"]
    return ModelMessage(
        "tool",
        json.dumps(_plain_json(result), ensure_ascii=False, separators=(",", ":")),
        name=str(event.data["name"]),
        tool_call_id=str(event.data["call_id"]),
    )
