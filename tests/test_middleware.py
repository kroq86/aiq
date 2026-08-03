from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from aiq import (
    Agent,
    DurableDispatcher,
    DurableEffectDispatcher,
    DurableModelLoop,
    Event,
    FunctionTool,
    InMemoryEventStore,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolRequest,
    run_stream_id,
)
from aiq.middleware import (
    MiddlewareExecutionError,
    ModelCallContext,
    apply_after_model,
)


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class State:
    pass


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


class RecordingMiddleware:
    def __init__(self, middleware_id: str, calls: list[str]) -> None:
        self.middleware_id = middleware_id
        self.calls = calls

    def before_model(self, context, request):
        self.calls.append(f"{self.middleware_id}:before_model:{context.operation_id}")
        last = request.messages[-1]
        changed = ModelMessage(last.role, f"{last.content}|bm:{self.middleware_id}")
        return ModelRequest(request.messages[:-1] + (changed,), request.tools, request.model)

    async def after_model(self, context, response):
        self.calls.append(f"{self.middleware_id}:after_model:{context.operation_id}")
        changed = ModelMessage(
            response.message.role,
            f"{response.message.content}|am:{self.middleware_id}",
        )
        return ModelResponse(
            changed,
            response.tool_calls,
            response.usage,
            response.provider_request_id,
        )

    def before_tool(self, context, request):
        self.calls.append(f"{self.middleware_id}:before_tool:{context.operation_id}")
        city = str(request.call.arguments["city"])
        call = ToolCall(
            request.call.call_id,
            request.call.name,
            {"city": f"{city}|bt:{self.middleware_id}"},
        )
        return ToolRequest(call, request.expected_definition)

    async def after_tool(self, context, result):
        self.calls.append(f"{self.middleware_id}:after_tool:{context.operation_id}")
        return {**result, f"at_{self.middleware_id}": True}


class Provider:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request, *, operation_id):
        self.requests.append((request, operation_id))
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "done"))
        return ModelResponse(
            ModelMessage("assistant", "calling"),
            (ToolCall("call-1", "weather", {"city": "Tbilisi"}),),
        )


def build_agent(*, middleware=()):
    agent = Agent(name="assistant", version="middleware-1", initial_state=State)

    @agent.event
    @dataclass(frozen=True)
    class Started:
        text: str

    tools = ToolRegistry()
    tool_arguments = []

    async def weather(city: str):
        tool_arguments.append(city)
        return {"city": city}

    tools.register(FunctionTool(WEATHER, weather))
    provider = Provider()
    loop = DurableModelLoop(
        start_on=Started,
        build_request=lambda state, event, definitions: ModelRequest(
            (ModelMessage("user", event.text),), definitions
        ),
        tool_definitions=(WEATHER,),
        provider="model",
        tools="tools",
        middleware=middleware,
    )
    loop.install(agent)
    return agent, loop, provider, tools, tool_arguments, Started


def drive(agent, provider, tools, initial_event):
    store = InMemoryEventStore()
    runtime = agent.build_runtime(context={"model": provider, "tools": tools})
    stream_id = run_stream_id("assistant", "middleware")
    run(
        store.append(
            stream_id,
            -1,
            (
                Event(
                    "RunCreated",
                    {"agent": "assistant", "definition_version": "middleware-1"},
                ),
                Event(type(initial_event).__name__, {"text": initial_event.text}),
            ),
        )
    )
    reactions = DurableDispatcher(
        agent=runtime.agent, store=store, subscription_name="middleware:reactions"
    )
    effects = DurableEffectDispatcher(
        agent=runtime.agent,
        store=store,
        effects=runtime.effects,
        context=runtime.context,
        subscription_name="middleware:effects",
    )
    for _ in range(40):
        if not (run(reactions.run_once()) | run(effects.run_once())):
            break
    return run(store.load(stream_id))


class MiddlewareTests(unittest.TestCase):
    def test_after_model_cannot_change_provider_response_identity(self) -> None:
        class IdentityChanging(RecordingMiddleware):
            async def after_model(self, context, response):
                del context
                return ModelResponse(
                    response.message,
                    response.tool_calls,
                    response.usage,
                    "changed-provider-request",
                )

        middleware = IdentityChanging("identity", [])
        response = ModelResponse(
            ModelMessage("assistant", "answer"),
            provider_request_id="original-provider-request",
        )
        with self.assertRaises(MiddlewareExecutionError):
            run(
                apply_after_model(
                    (middleware,), ModelCallContext("model", 1, 0, "operation"), response
                )
            )

    def test_chain_order_and_effective_values_are_durable(self) -> None:
        calls = []
        middleware = (
            RecordingMiddleware("a", calls),
            RecordingMiddleware("b", calls),
        )
        agent, loop, provider, tools, tool_arguments, Started = build_agent(
            middleware=middleware
        )
        history = drive(agent, provider, tools, Started("hello"))

        def event_type(item):
            return item.event.event_type

        first_model_request = next(
            item for item in history if event_type(item) == loop.events.ModelCallRequested.__name__
        )
        tool_request = next(
            item for item in history if event_type(item) == loop.events.ToolCallRequested.__name__
        )
        tool_result = next(
            item for item in history if event_type(item) == loop.events.ToolCallSucceeded.__name__
        )
        first_model_result = next(
            item for item in history if event_type(item) == loop.events.ModelCallSucceeded.__name__
        )

        self.assertEqual(
            first_model_request.event.data["request"]["messages"][-1]["content"],
            "hello|bm:a|bm:b",
        )
        self.assertEqual(provider.requests[0][0].messages[-1].content, "hello|bm:a|bm:b")
        self.assertEqual(
            first_model_result.event.data["response"]["message"]["content"],
            "calling|am:b|am:a",
        )
        self.assertEqual(
            tool_request.event.data["call"]["arguments"]["city"],
            "Tbilisi|bt:a|bt:b",
        )
        self.assertEqual(tool_arguments, ["Tbilisi|bt:a|bt:b"])
        self.assertEqual(
            tool_result.event.data["result"],
            {"city": "Tbilisi|bt:a|bt:b", "at_b": True, "at_a": True},
        )
        self.assertTrue(all(entry.endswith(":None") for entry in calls if "before_" in entry))
        self.assertTrue(all(not entry.endswith(":None") for entry in calls if ":after_" in entry))

    def test_before_failure_is_committed_without_external_invocation(self) -> None:
        class Failing(RecordingMiddleware):
            def before_model(self, context, request):
                raise RuntimeError("blocked")

        agent, loop, provider, tools, _, Started = build_agent(
            middleware=(Failing("guard", []),)
        )
        history = drive(agent, provider, tools, Started("hello"))
        event_types = [item.event.event_type for item in history]
        self.assertEqual(provider.requests, [])
        self.assertNotIn(loop.events.ModelCallRequested.__name__, event_types)
        self.assertIn(loop.events.MiddlewareFailed.__name__, event_types)
        failed = next(
            item.event for item in history if item.event.event_type == loop.events.MiddlewareFailed.__name__
        )
        self.assertEqual(failed.data["middleware_id"], "guard")
        self.assertEqual(failed.data["phase"], "before_model")
        self.assertEqual(event_types[-1], loop.events.RunFailed.__name__)

    def test_tool_identity_change_is_a_lifecycle_failure(self) -> None:
        class IdentityChanging(RecordingMiddleware):
            def before_tool(self, context, request):
                return ToolRequest(
                    ToolCall("different", request.call.name, request.call.arguments),
                    request.expected_definition,
                )

        agent, loop, provider, tools, tool_arguments, Started = build_agent(
            middleware=(IdentityChanging("identity", []),)
        )
        history = drive(agent, provider, tools, Started("hello"))
        event_types = [item.event.event_type for item in history]
        self.assertEqual(tool_arguments, [])
        self.assertNotIn(loop.events.ToolCallRequested.__name__, event_types)
        self.assertIn(loop.events.MiddlewareFailed.__name__, event_types)

    def test_after_model_failure_is_committed_after_one_provider_invocation(self) -> None:
        class Failing(RecordingMiddleware):
            async def after_model(self, context, response):
                raise RuntimeError("response blocked")

        agent, loop, provider, tools, tool_arguments, Started = build_agent(
            middleware=(Failing("response-filter", []),)
        )
        history = drive(agent, provider, tools, Started("hello"))
        event_types = [item.event.event_type for item in history]
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(tool_arguments, [])
        self.assertNotIn(loop.events.ModelCallSucceeded.__name__, event_types)
        self.assertIn(loop.events.MiddlewareFailed.__name__, event_types)
        self.assertEqual(event_types[-1], loop.events.RunFailed.__name__)

    def test_after_tool_failure_is_committed_after_one_tool_invocation(self) -> None:
        class Failing(RecordingMiddleware):
            async def after_tool(self, context, result):
                raise RuntimeError("tool result blocked")

        agent, loop, provider, tools, tool_arguments, Started = build_agent(
            middleware=(Failing("tool-filter", []),)
        )
        history = drive(agent, provider, tools, Started("hello"))
        event_types = [item.event.event_type for item in history]
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(tool_arguments, ["Tbilisi|bt:tool-filter"])
        self.assertNotIn(loop.events.ToolCallSucceeded.__name__, event_types)
        self.assertIn(loop.events.MiddlewareFailed.__name__, event_types)
        self.assertEqual(event_types[-1], loop.events.RunFailed.__name__)


if __name__ == "__main__":
    unittest.main()
