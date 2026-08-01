"""Typed, in-process lifecycle middleware for ``DurableModelLoop``.

Middleware transforms values at the model/tool boundary.  It does not own
durability, retries, provider/tool invocation, or event identity.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from .core import Event, JsonValue
from .models import ModelRequest, ModelResponse, ToolCall, ToolDefinition

ToolResult: TypeAlias = JsonValue


@dataclass(frozen=True, slots=True)
class ModelCallContext:
    policy_name: str
    model_step: int
    tool_calls_used: int
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    policy_name: str
    model_step: int
    tool_calls_used: int
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRequest:
    call: ToolCall
    expected_definition: ToolDefinition


class AgentMiddleware(Protocol):
    @property
    def middleware_id(self) -> str: ...

    def before_model(
        self, context: ModelCallContext, request: ModelRequest
    ) -> ModelRequest: ...

    async def after_model(
        self, context: ModelCallContext, response: ModelResponse
    ) -> ModelResponse: ...

    def before_tool(
        self, context: ToolCallContext, request: ToolRequest
    ) -> ToolRequest: ...

    async def after_tool(
        self, context: ToolCallContext, result: ToolResult
    ) -> ToolResult: ...


class MiddlewareExecutionError(RuntimeError):
    """A middleware hook failed before its transformed value was committed."""

    def __init__(self, *, middleware_id: str, phase: str, reason: str) -> None:
        self.middleware_id = middleware_id
        self.phase = phase
        self.reason = reason
        super().__init__(f"middleware {middleware_id!r} failed in {phase}: {reason}")


def validate_middleware(middleware: tuple[AgentMiddleware, ...]) -> None:
    seen: set[str] = set()
    for item in middleware:
        middleware_id = getattr(item, "middleware_id", None)
        if not isinstance(middleware_id, str) or not middleware_id:
            raise TypeError("middleware_id must be a non-empty string")
        if middleware_id in seen:
            raise ValueError(f"duplicate middleware_id: {middleware_id!r}")
        seen.add(middleware_id)
        for phase in ("before_model", "after_model", "before_tool", "after_tool"):
            hook = getattr(item, phase, None)
            if not callable(hook):
                raise TypeError(f"middleware {middleware_id!r} has no callable {phase}")
        for phase in ("before_model", "before_tool"):
            if inspect.iscoroutinefunction(getattr(item, phase)):
                raise TypeError(
                    f"middleware {middleware_id!r} {phase} must be synchronous"
                )


def _failure(item: AgentMiddleware, phase: str, error: Exception) -> MiddlewareExecutionError:
    return MiddlewareExecutionError(
        middleware_id=item.middleware_id,
        phase=phase,
        reason=str(error) or type(error).__name__,
    )


def apply_before_model(
    middleware: tuple[AgentMiddleware, ...],
    context: ModelCallContext,
    request: ModelRequest,
) -> ModelRequest:
    current = request
    for item in middleware:
        try:
            transformed = item.before_model(context, current)
            if inspect.isawaitable(transformed):
                if inspect.iscoroutine(transformed):
                    transformed.close()
                raise TypeError("before_model must return ModelRequest synchronously")
            if not isinstance(transformed, ModelRequest):
                raise TypeError("before_model must return ModelRequest")
            current = transformed
        except Exception as error:
            if isinstance(error, MiddlewareExecutionError):
                raise
            raise _failure(item, "before_model", error) from error
    return current


async def apply_after_model(
    middleware: tuple[AgentMiddleware, ...],
    context: ModelCallContext,
    response: ModelResponse,
) -> ModelResponse:
    current = response
    original_provider_request_id = response.provider_request_id
    for item in reversed(middleware):
        try:
            pending: Awaitable[ModelResponse] = item.after_model(context, current)
            transformed = await pending
            if not isinstance(transformed, ModelResponse):
                raise TypeError("after_model must return ModelResponse")
            if transformed.provider_request_id != original_provider_request_id:
                raise ValueError("after_model must preserve model response identity")
            current = transformed
        except Exception as error:
            if isinstance(error, MiddlewareExecutionError):
                raise
            raise _failure(item, "after_model", error) from error
    return current


def apply_before_tool(
    middleware: tuple[AgentMiddleware, ...],
    context: ToolCallContext,
    request: ToolRequest,
) -> ToolRequest:
    current = request
    original_call_id = request.call.call_id
    original_name = request.call.name
    original_definition = request.expected_definition
    for item in middleware:
        try:
            transformed = item.before_tool(context, current)
            if inspect.isawaitable(transformed):
                if inspect.iscoroutine(transformed):
                    transformed.close()
                raise TypeError("before_tool must return ToolRequest synchronously")
            if not isinstance(transformed, ToolRequest):
                raise TypeError("before_tool must return ToolRequest")
            if (
                transformed.call.call_id != original_call_id
                or transformed.call.name != original_name
            ):
                raise ValueError("before_tool must preserve tool call identity")
            if transformed.expected_definition != original_definition:
                raise ValueError("before_tool must preserve the expected tool definition")
            current = transformed
        except Exception as error:
            if isinstance(error, MiddlewareExecutionError):
                raise
            raise _failure(item, "before_tool", error) from error
    return current


async def apply_after_tool(
    middleware: tuple[AgentMiddleware, ...],
    context: ToolCallContext,
    result: ToolResult,
) -> ToolResult:
    current = result
    for item in reversed(middleware):
        try:
            current = await item.after_tool(context, current)
            current = Event("MiddlewareToolResultValidated", {"result": current}).data[
                "result"
            ]
        except Exception as error:
            if isinstance(error, MiddlewareExecutionError):
                raise
            raise _failure(item, "after_tool", error) from error
    return current
