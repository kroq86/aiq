from __future__ import annotations

import asyncio
import unittest

from agentlog import (
    FunctionTool,
    ToolArgumentsRejected,
    ToolDefinition,
    ToolRegistry,
    tool_definition_fingerprint,
)


WEATHER = ToolDefinition(
    "weather",
    "Current weather",
    {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
)


class ToolTests(unittest.TestCase):
    def test_registry_builds_function_tools_and_fingerprints_ignore_key_order(self) -> None:
        def weather(city: str) -> dict:
            """Current weather."""
            return {"city": city}

        registry = ToolRegistry.from_functions(weather)
        definition = registry.definitions()[0]
        reordered = ToolDefinition(
            definition.name,
            definition.description,
            {
                "required": ["city"],
                "properties": {"city": {"type": "string"}},
                "additionalProperties": False,
                "type": "object",
            },
        )
        self.assertEqual(
            tool_definition_fingerprint(definition),
            tool_definition_fingerprint(reordered),
        )

    def test_registry_resolves_but_does_not_execute_tools(self) -> None:
        calls: list[str] = []

        async def weather(city: str):
            calls.append(city)
            return {"city": city, "temperature": 23}

        tool = FunctionTool(WEATHER, weather)
        registry = ToolRegistry()
        self.assertIs(registry.register(tool), tool)
        self.assertEqual(registry.definitions(), (WEATHER,))
        self.assertIs(registry.validate("weather", {"city": "Tbilisi"}), tool)
        self.assertEqual(calls, [])

        result = asyncio.run(
            registry.resolve("weather").execute(
                {"city": "Tbilisi"}, operation_id="operation-1"
            )
        )
        self.assertEqual(dict(result), {"city": "Tbilisi", "temperature": 23})
        self.assertEqual(calls, ["Tbilisi"])

    def test_duplicate_unknown_and_invalid_arguments_are_distinct(self) -> None:
        registry = ToolRegistry()
        tool = FunctionTool(WEATHER, lambda city: {"city": city})
        registry.register(tool)
        with self.assertRaises(ValueError):
            registry.register(tool)
        with self.assertRaises(LookupError):
            registry.resolve("missing")
        with self.assertRaises(ToolArgumentsRejected):
            registry.validate("weather", {})
        with self.assertRaises(ToolArgumentsRejected):
            registry.validate("weather", {"city": "Tbilisi", "units": "C"})


if __name__ == "__main__":
    unittest.main()
