from __future__ import annotations

import subprocess
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

try:
    from mcp.types import CallToolResult, TextContent
except ImportError as error:  # pragma: no cover - optional test dependency
    raise unittest.SkipTest(
        "MCP adapter tests require the 'mcp' extra: pip install -e '.[mcp,test]'"
    ) from error

from aiq import MCPTool, ToolDefinition, ToolExecutionFailed, ToolRegistry


def definition() -> ToolDefinition:
    return ToolDefinition(
        "echo",
        "Echo one value",
        {
            "type": "object",
            "properties": {"value": {"type": "array"}},
            "required": ("value",),
            "additionalProperties": False,
        },
    )


class FakeHTTPClient:
    instances: list[FakeHTTPClient] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self) -> FakeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True


class FakeSession:
    result = CallToolResult(content=[], structuredContent={"ok": True})
    calls: list[tuple[str, dict[str, object], object]] = []
    initialized = False
    closed = False

    def __init__(self, read: object, write: object, *, read_timeout_seconds: object):
        self.read = read
        self.write = write
        self.read_timeout_seconds = read_timeout_seconds

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        type(self).closed = True

    async def initialize(self) -> None:
        type(self).initialized = True

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        read_timeout_seconds: object,
    ) -> CallToolResult:
        type(self).calls.append((name, arguments, read_timeout_seconds))
        return type(self).result


@asynccontextmanager
async def fake_transport(url: str, *, http_client: object):
    del url, http_client
    yield object(), object(), lambda: None


class MCPToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeHTTPClient.instances.clear()
        FakeSession.calls.clear()
        FakeSession.initialized = False
        FakeSession.closed = False
        FakeSession.result = CallToolResult(
            content=[], structuredContent={"ok": True}
        )

    async def execute(
        self,
        result: CallToolResult,
        *,
        operation_id_argument: str | None = None,
    ):
        FakeSession.result = result
        tool = MCPTool(
            definition(),
            url="http://mcp.example/mcp",
            timeout=3.5,
            operation_id_argument=operation_id_argument,
        )
        with (
            patch("aiq.mcp.httpx.AsyncClient", FakeHTTPClient),
            patch("aiq.mcp.streamable_http_client", fake_transport),
            patch("aiq.mcp.ClientSession", FakeSession),
        ):
            value = await tool.execute(
                {"value": ("one", "two")},
                operation_id="operation-1",
            )
        return tool, value

    async def test_structured_content_is_frozen_and_resources_close(self) -> None:
        tool, value = await self.execute(
            CallToolResult(
                content=[],
                structuredContent={"items": [1, 2], "ok": True},
            )
        )

        self.assertEqual(value, {"items": (1, 2), "ok": True})
        self.assertEqual(tool.definition.name, "echo")
        self.assertTrue(FakeSession.initialized)
        self.assertTrue(FakeSession.closed)
        self.assertEqual(FakeHTTPClient.instances[0].timeout, 3.5)
        self.assertTrue(FakeHTTPClient.instances[0].closed)
        name, payload, read_timeout = FakeSession.calls[0]
        self.assertEqual(name, "echo")
        self.assertEqual(payload, {"value": ["one", "two"]})
        self.assertEqual(read_timeout.total_seconds(), 3.5)

    async def test_json_text_fallback_is_accepted(self) -> None:
        _, value = await self.execute(
            CallToolResult(
                content=[TextContent(type="text", text='{"answer":[1,2]}')]
            )
        )

        self.assertEqual(value, {"answer": (1, 2)})

    async def test_operation_id_can_be_injected_outside_model_schema(self) -> None:
        _, value = await self.execute(
            CallToolResult(content=[], structuredContent={"ok": True}),
            operation_id_argument="operation_id",
        )

        self.assertEqual(value, {"ok": True})
        self.assertEqual(
            FakeSession.calls[0][1],
            {"value": ["one", "two"], "operation_id": "operation-1"},
        )

    async def test_mcp_error_is_tool_execution_failure(self) -> None:
        with self.assertRaisesRegex(ToolExecutionFailed, "MCP tool failed"):
            await self.execute(
                CallToolResult(
                    content=[TextContent(type="text", text="denied")],
                    isError=True,
                )
            )

    async def test_invalid_text_result_is_tool_execution_failure(self) -> None:
        with self.assertRaisesRegex(ToolExecutionFailed, "invalid JSON"):
            await self.execute(
                CallToolResult(
                    content=[TextContent(type="text", text="not-json")]
                )
            )

    async def test_multiple_text_blocks_are_not_guessed(self) -> None:
        with self.assertRaisesRegex(ToolExecutionFailed, "one JSON text block"):
            await self.execute(
                CallToolResult(
                    content=[
                        TextContent(type="text", text='{"one":1}'),
                        TextContent(type="text", text='{"two":2}'),
                    ]
                )
            )

    async def test_transport_failure_is_tool_execution_failure(self) -> None:
        @asynccontextmanager
        async def failing_transport(url: str, *, http_client: object):
            del url, http_client
            raise RuntimeError("connection refused")
            yield  # pragma: no cover

        tool = MCPTool(definition(), url="http://mcp.example/mcp")
        with (
            patch("aiq.mcp.httpx.AsyncClient", FakeHTTPClient),
            patch("aiq.mcp.streamable_http_client", failing_transport),
            self.assertRaisesRegex(ToolExecutionFailed, "connection refused"),
        ):
            await tool.execute({"value": ()}, operation_id="operation-1")
        self.assertTrue(FakeHTTPClient.instances[0].closed)

    def test_registry_accepts_adapter_and_validates_model_arguments(self) -> None:
        tool = MCPTool(definition(), url="http://mcp.example/mcp")
        registry = ToolRegistry()

        self.assertIs(registry.register(tool), tool)
        self.assertIs(registry.validate("echo", {"value": ()}), tool)

    def test_constructor_rejects_unsafe_or_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            MCPTool(definition(), url="")
        with self.assertRaisesRegex(ValueError, "must be positive"):
            MCPTool(definition(), url="http://mcp.example/mcp", timeout=0)
        with self.assertRaisesRegex(ValueError, "must not be exposed"):
            MCPTool(
                definition(),
                url="http://mcp.example/mcp",
                operation_id_argument="value",
            )

    def test_top_level_import_does_not_require_mcp_extra(self) -> None:
        script = """
import builtins
real_import = builtins.__import__
def deny_mcp(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and (name == "mcp" or name.startswith("mcp.")):
        raise ImportError("blocked MCP dependency")
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = deny_mcp
import aiq
assert aiq.Event
try:
    aiq.MCPTool
except ImportError as error:
    assert "aiq[mcp]" in str(error)
else:
    raise AssertionError("MCPTool import unexpectedly succeeded")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
