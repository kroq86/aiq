"""Typed tool catalog and local Python-function adapter."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, get_type_hints

from .core import Event, JsonValue
from .models import ToolDefinition


class ToolArgumentsRejected(ValueError):
    """Arguments do not satisfy a tool's declared input contract."""


class ToolExecutionFailed(RuntimeError):
    """An expected tool failure occurred after execution began."""


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        *,
        operation_id: str,
    ) -> JsonValue: ...


ToolFunction = Callable[..., JsonValue | Awaitable[JsonValue]]


def _plain_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def tool_definition_fingerprint(definition: ToolDefinition) -> str:
    """Canonical, order-independent signature for definition/resource drift."""
    return json.dumps(
        {
            "description": definition.description,
            "input_schema": _plain_json(definition.input_schema),
            "name": definition.name,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _matches_type(value: JsonValue, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, tuple)
    if expected == "null":
        return value is None
    raise ToolArgumentsRejected(f"unsupported JSON schema type: {expected!r}")


def validate_tool_arguments(
    definition: ToolDefinition,
    arguments: Mapping[str, JsonValue],
) -> None:
    """Validate the small JSON-schema subset promised by the 0.2 adapter.

    Supported: top-level object, properties/type, required, and
    additionalProperties=false.  More advanced schemas belong in a future
    validator adapter instead of being silently approximated here.
    """
    schema = definition.input_schema
    if schema.get("type", "object") != "object":
        raise ToolArgumentsRejected("tool input schema must describe an object")
    properties = schema.get("properties", {})
    required = schema.get("required", ())
    if not isinstance(properties, Mapping) or not isinstance(required, tuple):
        raise ToolArgumentsRejected("invalid tool input schema")
    missing = [str(name) for name in required if name not in arguments]
    if missing:
        raise ToolArgumentsRejected(f"missing required arguments: {missing}")
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(arguments) - set(properties))
        if unexpected:
            raise ToolArgumentsRejected(f"unexpected arguments: {unexpected}")
    for name, value in arguments.items():
        property_schema = properties.get(name)
        if property_schema is None:
            continue
        if not isinstance(property_schema, Mapping):
            raise ToolArgumentsRejected(f"invalid schema for argument {name!r}")
        expected = property_schema.get("type")
        if expected is not None and (
            not isinstance(expected, str) or not _matches_type(value, expected)
        ):
            raise ToolArgumentsRejected(
                f"argument {name!r} must have JSON type {expected!r}"
            )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool | ToolFunction) -> Tool:
        if callable(tool) and not hasattr(tool, "definition"):
            tool = function_tool(tool)
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name!r}")
        self._tools[name] = tool
        return tool

    def resolve(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise LookupError(f"unknown tool: {name!r}") from error

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def definition_fingerprints(self) -> frozenset[str]:
        return frozenset(tool_definition_fingerprint(item) for item in self.definitions())

    @classmethod
    def from_functions(cls, *functions: ToolFunction) -> "ToolRegistry":
        registry = cls()
        for function in functions:
            registry.register(function)
        return registry

    def validate(self, name: str, arguments: Mapping[str, JsonValue]) -> Tool:
        tool = self.resolve(name)
        validate_tool_arguments(tool.definition, arguments)
        return tool


def _annotation_schema(annotation: Any) -> Mapping[str, JsonValue]:
    json_type = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        list: "array",
        tuple: "array",
    }.get(annotation)
    if json_type is None:
        raise TypeError(f"unsupported tool parameter annotation: {annotation!r}")
    return {"type": json_type}


def function_tool(function: ToolFunction) -> "FunctionTool":
    signature = inspect.signature(function)
    hints = get_type_hints(function)
    properties: dict[str, JsonValue] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind not in (
            parameter.POSITIONAL_OR_KEYWORD,
            parameter.KEYWORD_ONLY,
        ):
            raise TypeError("function tools accept only named parameters")
        annotation = hints.get(name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            raise TypeError(f"tool parameter {name!r} requires a type annotation")
        properties[name] = _annotation_schema(annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return FunctionTool(
        ToolDefinition(
            function.__name__,
            (inspect.getdoc(function) or function.__name__).splitlines()[0],
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        ),
        function,
    )


@dataclass(frozen=True, slots=True)
class FunctionTool:
    definition: ToolDefinition
    function: ToolFunction

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        *,
        operation_id: str,
    ) -> JsonValue:
        del operation_id  # available to custom Tool adapters; local functions need not accept it
        validate_tool_arguments(self.definition, arguments)
        try:
            inspect.signature(self.function).bind(**dict(arguments))
        except TypeError as error:
            raise ToolArgumentsRejected(str(error)) from error
        result = self.function(**dict(arguments))
        if inspect.isawaitable(result):
            result = await result
        # Reuse Event's recursive validation and return its frozen value.
        return Event("ToolResultValidated", {"result": result}).data["result"]
