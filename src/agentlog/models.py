"""Provider-neutral values for one durable model call.

The protocol deliberately represents one external request.  It does not own
conversation state, execute tools, or continue a reason-act loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from .core import Event, JsonValue


class ModelCallRejectedError(ValueError):
    """A request must not be sent to the provider."""


class ModelCallFailedError(RuntimeError):
    """An expected provider failure occurred after execution began."""


class ModelOutputRejectedError(ValueError):
    """The provider returned an observation the policy cannot consume."""


def _json_mapping(value: Mapping[str, JsonValue], *, field_name: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    # Event owns the canonical recursive JSON validation/freezing rules.
    frozen = Event("ValueValidated", {"value": value}).data["value"]
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported model message role: {self.role!r}")
        if not isinstance(self.content, str):
            raise TypeError("message content must be a string")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")

    def to_data(self) -> Mapping[str, JsonValue]:
        data: dict[str, JsonValue] = {"role": self.role, "content": self.content}
        if self.name is not None:
            data["name"] = self.name
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        return MappingProxyType(data)

    @classmethod
    def from_data(cls, data: Mapping[str, JsonValue]) -> "ModelMessage":
        return cls(
            role=str(data["role"]),
            content=str(data["content"]),
            name=str(data["name"]) if data.get("name") is not None else None,
            tool_call_id=(
                str(data["tool_call_id"])
                if data.get("tool_call_id") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool name must not be empty")
        if not self.description:
            raise ValueError("tool description must not be empty")
        object.__setattr__(
            self,
            "input_schema",
            _json_mapping(self.input_schema, field_name="tool input_schema"),
        )

    def to_data(self) -> Mapping[str, JsonValue]:
        return MappingProxyType(
            {
                "name": self.name,
                "description": self.description,
                "input_schema": self.input_schema,
            }
        )

    @classmethod
    def from_data(cls, data: Mapping[str, JsonValue]) -> "ToolDefinition":
        schema = data["input_schema"]
        if not isinstance(schema, Mapping):
            raise TypeError("tool input_schema must be a mapping")
        return cls(str(data["name"]), str(data["description"]), schema)


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.call_id or not self.name:
            raise ValueError("tool call_id and name must not be empty")
        object.__setattr__(
            self,
            "arguments",
            _json_mapping(self.arguments, field_name="tool arguments"),
        )

    def to_data(self) -> Mapping[str, JsonValue]:
        return MappingProxyType(
            {"call_id": self.call_id, "name": self.name, "arguments": self.arguments}
        )

    @classmethod
    def from_data(cls, data: Mapping[str, JsonValue]) -> "ToolCall":
        arguments = data["arguments"]
        if not isinstance(arguments, Mapping):
            raise TypeError("tool arguments must be a mapping")
        return cls(str(data["call_id"]), str(data["name"]), arguments)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")

    def to_data(self) -> Mapping[str, JsonValue]:
        return MappingProxyType(
            {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }
        )


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()
    model: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if not self.messages:
            raise ValueError("model request requires at least one message")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("model request contains duplicate tool names")

    def to_data(self) -> Mapping[str, JsonValue]:
        return MappingProxyType(
            {
                "messages": tuple(message.to_data() for message in self.messages),
                "tools": tuple(tool.to_data() for tool in self.tools),
                "model": self.model,
            }
        )

    @classmethod
    def from_data(cls, data: Mapping[str, JsonValue]) -> "ModelRequest":
        messages = data.get("messages")
        tools = data.get("tools", ())
        if not isinstance(messages, tuple) or not isinstance(tools, tuple):
            raise TypeError("serialized model messages and tools must be arrays")
        return cls(
            messages=tuple(ModelMessage.from_data(item) for item in messages),
            tools=tuple(ToolDefinition.from_data(item) for item in tools),
            model=str(data["model"]) if data.get("model") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ModelResponse:
    message: ModelMessage
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        call_ids = [call.call_id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("model response contains duplicate tool call ids")

    def to_data(self) -> Mapping[str, JsonValue]:
        return MappingProxyType(
            {
                "message": self.message.to_data(),
                "tool_calls": tuple(call.to_data() for call in self.tool_calls),
                "usage": self.usage.to_data() if self.usage is not None else None,
                "provider_request_id": self.provider_request_id,
            }
        )

    @classmethod
    def from_data(cls, data: Mapping[str, JsonValue]) -> "ModelResponse":
        message = data["message"]
        tool_calls = data.get("tool_calls", ())
        usage = data.get("usage")
        if not isinstance(message, Mapping) or not isinstance(tool_calls, tuple):
            raise TypeError("invalid serialized model response")
        parsed_usage = None
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise TypeError("model usage must be an object")
            parsed_usage = ModelUsage(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        return cls(
            message=ModelMessage.from_data(message),
            tool_calls=tuple(ToolCall.from_data(item) for item in tool_calls),
            usage=parsed_usage,
            provider_request_id=(
                str(data["provider_request_id"])
                if data.get("provider_request_id") is not None
                else None
            ),
        )


class ModelProvider(Protocol):
    async def complete(
        self,
        request: ModelRequest,
        *,
        operation_id: str,
    ) -> ModelResponse: ...
