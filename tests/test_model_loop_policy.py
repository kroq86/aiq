from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, replace

from agentlog import (
    Agent,
    DefinitionError,
    DefinitionResourceMismatch,
    DurableDispatcher,
    DurableEffectDispatcher,
    DurableModelLoop,
    InMemoryEventStore,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    run_stream_id,
)


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class State:
    answer: str | None = None


class Provider:
    async def complete(self, request, *, operation_id):
        del operation_id
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "23 C"))
        return ModelResponse(
            ModelMessage("assistant", "Checking"),
            (ToolCall("weather-1", "get_weather", {"city": "Tbilisi"}),),
        )


def get_weather(city: str) -> dict:
    """Current weather."""
    return {"city": city, "temperature": 23}


def define(tools: ToolRegistry):
    agent = Agent(name="assistant", version="1", initial_state=State)

    @agent.event
    @dataclass(frozen=True)
    class UserMessageAdded:
        text: str

    loop = DurableModelLoop(
        start_on=UserMessageAdded,
        build_request=lambda state, event, definitions: ModelRequest(
            (ModelMessage("user", event.text),), definitions
        ),
        tool_definitions=tools.definitions(),
        provider="model",
        tools="tools",
    )
    loop.install(agent)

    @agent.reduce(loop.events.AnswerProduced)
    def answer(state, event):
        return replace(state, answer=event.answer)

    @agent.command("message")
    def message(payload):
        return UserMessageAdded(str(payload["text"]))

    return agent, loop


class ModelLoopPolicyTests(unittest.TestCase):
    def test_compiled_runtime_handlers_do_not_retain_policy_instance(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)
        runtime = agent.build_runtime(context={"model": Provider(), "tools": tools})
        handlers = tuple(runtime.agent._reactions.values()) + tuple(
            runtime.effects._handlers.values()
        )
        for handler in handlers:
            captured = tuple(
                cell.cell_contents for cell in (handler.__closure__ or ())
            )
            self.assertNotIn(loop, captured)

    def test_duplicate_instance_and_namespace_are_definition_errors(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)
        with self.assertRaises(DefinitionError):
            loop.install(agent)

        another = DurableModelLoop(
            start_on=loop.start_on,
            build_request=loop.build_request,
            tool_definitions=tools.definitions(),
            provider="model",
            tools="tools",
        )
        with self.assertRaises(DefinitionError):
            another.install(agent)

    def test_registration_rejects_changed_tool_definition(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, _ = define(tools)
        changed = ToolRegistry()
        changed.register(
            type(
                "ChangedTool",
                (),
                {
                    "definition": ToolDefinition(
                        "get_weather",
                        "Changed semantics",
                        tools.definitions()[0].input_schema,
                    ),
                    "execute": staticmethod(lambda arguments, operation_id: None),
                },
            )()
        )
        with self.assertRaises(DefinitionResourceMismatch) as raised:
            agent.build_runtime(context={"model": Provider(), "tools": changed})
        self.assertEqual(raised.exception.changed, ("get_weather",))

    def test_persisted_definition_drift_fails_before_tool_execution(self) -> None:
        store = InMemoryEventStore()
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)
        runtime = agent.build_runtime(context={"model": Provider(), "tools": tools})
        stream_id = run_stream_id("assistant", "drift")
        from agentlog import Event, FunctionTool

        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated", {"agent": "assistant", "definition_version": "1"}
                    ),
                    agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )
        reactions = DurableDispatcher(
            agent=runtime.agent,
            store=store,
            subscription_name="assistant:1:reactions",
        )
        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="assistant:1:effects",
        )
        for _ in range(20):
            run(reactions.run_once())
            history = run(store.load(stream_id))
            if history[-1].event.event_type == loop.events.ToolCallRequested.__name__:
                break
            run(effects.run_once())
        else:
            self.fail("tool request was not persisted")

        changed = ToolDefinition(
            "get_weather",
            "Changed after registration",
            tools.definitions()[0].input_schema,
        )
        tools._tools["get_weather"] = FunctionTool(changed, get_weather)
        with self.assertRaises(DefinitionResourceMismatch):
            for _ in range(20):
                run(effects.run_once())

    def test_fresh_runtime_and_resources_resume_from_persisted_continuation(self) -> None:
        store = InMemoryEventStore()
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)
        runtime = agent.build_runtime(context={"model": Provider(), "tools": tools})
        stream_id = run_stream_id("assistant", "policy-restart")
        run(
            store.append(
                stream_id,
                -1,
                (
                    __import__("agentlog").Event(
                        "RunCreated", {"agent": "assistant", "definition_version": "1"}
                    ),
                    agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )

        reactions = DurableDispatcher(
            agent=runtime.agent,
            store=store,
            subscription_name="assistant:1:reactions",
        )
        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="assistant:1:effects",
        )
        for _ in range(20):
            run(reactions.run_once())
            run(effects.run_once())
            history = run(store.load(stream_id))
            if history[-1].event.event_type == loop.events.ToolCallSucceeded.__name__:
                break
        else:
            self.fail("tool result was not persisted")

        # Recreate definition, policy, provider, and registry. Only store and
        # durable subscription checkpoints survive.
        fresh_tools = ToolRegistry.from_functions(get_weather)
        fresh_agent, fresh_loop = define(fresh_tools)
        fresh_runtime = fresh_agent.build_runtime(
            context={"model": Provider(), "tools": fresh_tools}
        )
        reactions = DurableDispatcher(
            agent=fresh_runtime.agent,
            store=store,
            subscription_name="assistant:1:reactions",
        )
        effects = DurableEffectDispatcher(
            agent=fresh_runtime.agent,
            store=store,
            effects=fresh_runtime.effects,
            context=fresh_runtime.context,
            subscription_name="assistant:1:effects",
        )
        for _ in range(30):
            if not (run(reactions.run_once()) | run(effects.run_once())):
                break
        history = run(store.load(stream_id))
        self.assertEqual(fresh_runtime.agent.rebuild(history).answer, "23 C")
        self.assertEqual(history[-1].event.event_type, fresh_loop.events.RunCompleted.__name__)


if __name__ == "__main__":
    unittest.main()
