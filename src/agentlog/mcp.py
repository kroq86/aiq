"""Streamable HTTP MCP adapter for Agentlog's existing ``Tool`` contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta

try:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import TextContent
except ImportError as error:  # pragma: no cover - depends on optional installation
    raise ImportError(
        "MCPTool requires the 'mcp' extra: pip install agentlog[mcp]"
    ) from error

from .core import Event, JsonValue
from .models import ToolDefinition
from .tools import ToolExecutionFailed


def _plain_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _validated_result(value: object) -> JsonValue:
    try:
        return Event("MCPToolResultValidated", {"result": value}).data["result"]
    except (TypeError, ValueError) as error:
        raise ToolExecutionFailed("MCP tool returned a non-JSON result") from error


class MCPTool:
    """Call one statically declared MCP tool over Streamable HTTP.

    Agentlog owns the durable request and committed outcome. This adapter owns
    only one physical MCP session/call and deliberately performs no retries or
    deduplication.
    """

    def __init__(
        self,
        definition: ToolDefinition,
        *,
        url: str,
        timeout: float = 10.0,
        operation_id_argument: str | None = None,
    ) -> None:
        if not url:
            raise ValueError("MCP URL must not be empty")
        if timeout <= 0:
            raise ValueError("MCP timeout must be positive")
        if operation_id_argument is not None:
            if not operation_id_argument:
                raise ValueError("operation_id_argument must not be empty")
            properties = definition.input_schema.get("properties", {})
            if (
                isinstance(properties, Mapping)
                and operation_id_argument in properties
            ):
                raise ValueError(
                    "operation_id_argument must not be exposed in the model schema"
                )
        self._definition = definition
        self._url = url
        self._timeout = timeout
        self._operation_id_argument = operation_id_argument

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        *,
        operation_id: str,
    ) -> JsonValue:
        payload = {
            key: _plain_json(value)
            for key, value in arguments.items()
        }
        if self._operation_id_argument is not None:
            payload[self._operation_id_argument] = operation_id

        read_timeout = timedelta(seconds=self._timeout)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http_client:
                async with streamable_http_client(
                    self._url, http_client=http_client
                ) as (read, write, _):
                    async with ClientSession(
                        read, write, read_timeout_seconds=read_timeout
                    ) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            self._definition.name,
                            payload,
                            read_timeout_seconds=read_timeout,
                        )
        except Exception as error:
            raise ToolExecutionFailed(f"MCP call failed: {error}") from error

        if result.isError:
            raise ToolExecutionFailed(f"MCP tool failed: {result.content}")

        value: object = result.structuredContent
        if value is None:
            if len(result.content) != 1 or not isinstance(
                result.content[0], TextContent
            ):
                raise ToolExecutionFailed(
                    "MCP tool returned neither structured content nor one JSON text block"
                )
            try:
                value = json.loads(result.content[0].text)
            except (TypeError, json.JSONDecodeError) as error:
                raise ToolExecutionFailed(
                    "MCP tool returned invalid JSON text content"
                ) from error
        return _validated_result(value)
