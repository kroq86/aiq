from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from aiq import (
    Agent,
    ArtifactBinding,
    DurableDispatcher,
    DurableEffectDispatcher,
    DurableModelLoop,
    Event,
    InMemoryArtifactStore,
    InMemoryEventStore,
    InstructionResolutionError,
    InstructionTemplate,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolRegistry,
    run_stream_id,
)


def run(coro):
    return asyncio.run(coro)


class InstructionTemplateTests(unittest.TestCase):
    def test_resolution_is_deterministic_and_round_trips_in_request(self):
        store = InMemoryArtifactStore()
        ref = run(store.put("policy", b"Be precise", media_type="text/plain"))
        template = InstructionTemplate(
            "{artifact:policy} for customer {input:customer_id}; flags={input:flags}",
            template_id="support-policy",
            version="1",
        )
        bindings = {
            "policy": ArtifactBinding(ref, "Be precise"),
        }
        inputs = {"customer_id": "C-17", "flags": {"b": 2, "a": 1}}
        first = template.resolve(inputs=inputs, artifacts=bindings)
        second = template.resolve(inputs=inputs, artifacts=bindings)
        self.assertEqual(first, second)
        self.assertEqual(
            first.text, 'Be precise for customer C-17; flags={"a":1,"b":2}'
        )
        request = ModelRequest(
            (ModelMessage("user", "help"),),
            artifacts=(ref,),
            instruction=first,
        )
        self.assertEqual(ModelRequest.from_data(request.to_data()), request)

    def test_strict_grammar_and_binding_set(self):
        with self.assertRaisesRegex(ValueError, "invalid placeholder"):
            InstructionTemplate(
                "{python:eval()}", template_id="unsafe", version="1"
            )
        template = InstructionTemplate(
            "Hello {input:name}", template_id="hello", version="1"
        )
        with self.assertRaisesRegex(InstructionResolutionError, "missing"):
            template.resolve()
        with self.assertRaisesRegex(InstructionResolutionError, "unexpected"):
            template.resolve(inputs={"name": "Ada", "environment": "secret"})

    def test_instruction_artifacts_must_be_pinned_in_request(self):
        store = InMemoryArtifactStore()
        ref = run(store.put("policy", b"Policy", media_type="text/plain"))
        instruction = InstructionTemplate(
            "{artifact:policy}", template_id="policy", version="1"
        ).resolve(artifacts={"policy": ArtifactBinding(ref, "Policy")})
        with self.assertRaisesRegex(ValueError, "must be pinned"):
            ModelRequest((ModelMessage("user", "help"),), instruction=instruction)


@dataclass(frozen=True)
class State:
    pass


def build_runtime_agent(build_request, provider, tools, artifacts):
    agent = Agent(name="instructions", version="1", initial_state=State)

    @agent.event
    @dataclass(frozen=True)
    class Started:
        pass

    loop = DurableModelLoop(
        start_on=Started,
        build_request=build_request,
        tool_definitions=tools.definitions(),
        provider="model",
        tools="tools",
        artifacts="artifacts",
    )
    loop.install(agent)
    runtime = agent.build_runtime(
        context={"model": provider, "tools": tools, "artifacts": artifacts}
    )
    return agent, loop, runtime, Started


def drive(agent, runtime, started):
    store = InMemoryEventStore()
    stream_id = run_stream_id("instructions", "run")
    run(
        store.append(
            stream_id,
            -1,
            (
                Event(
                    "RunCreated", {"agent": "instructions", "definition_version": "1"}
                ),
                Event(started.__name__, {}),
            ),
        )
    )
    reactions = DurableDispatcher(
        agent=runtime.agent, store=store, subscription_name="instructions:reactions"
    )
    effects = DurableEffectDispatcher(
        agent=runtime.agent,
        store=store,
        effects=runtime.effects,
        context=runtime.context,
        subscription_name="instructions:effects",
    )
    for _ in range(40):
        if not (run(reactions.run_once()) | run(effects.run_once())):
            break
    return run(store.load(stream_id))


class InstructionRuntimeTests(unittest.TestCase):
    def test_missing_binding_is_durable_failure_before_model_request(self):
        class Provider:
            calls = 0

            async def complete(self, request, *, operation_id):
                self.calls += 1
                return ModelResponse(ModelMessage("assistant", "unexpected"))

        provider = Provider()
        tools = ToolRegistry()
        artifacts = InMemoryArtifactStore()
        template = InstructionTemplate(
            "Hello {input:name}", template_id="hello", version="1"
        )
        agent, loop, runtime, Started = build_runtime_agent(
            lambda state, event, definitions: ModelRequest(
                (ModelMessage("user", "hello"),), instruction=template.resolve()
            ),
            provider,
            tools,
            artifacts,
        )
        history = drive(agent, runtime, Started)
        event_types = [item.event.event_type for item in history]
        self.assertEqual(provider.calls, 0)
        self.assertNotIn(loop.events.ModelCallRequested.__name__, event_types)
        self.assertIn(loop.events.InstructionResolutionFailed.__name__, event_types)
        self.assertEqual(event_types[-1], loop.events.RunFailed.__name__)

    def test_continuation_reuses_committed_instruction_after_bindings_change(self):
        artifacts = InMemoryArtifactStore()
        v1 = run(
            artifacts.put(
                "policy", b"Policy one", media_type="text/plain", version="1"
            )
        )
        v2 = run(
            artifacts.put(
                "policy", b"Policy two", media_type="text/plain", version="2"
            )
        )
        current = {"binding": ArtifactBinding(v1, "Policy one")}
        template = InstructionTemplate(
            "Use {artifact:policy}", template_id="policy", version="1"
        )
        tools = ToolRegistry()

        async def weather(city: str):
            return {"city": city}

        tools.register(weather)

        class Provider:
            def __init__(self):
                self.instructions = []

            async def complete(self, request, *, operation_id):
                self.instructions.append(request.instruction)
                if request.messages[-1].role == "tool":
                    return ModelResponse(ModelMessage("assistant", "done"))
                current["binding"] = ArtifactBinding(v2, "Policy two")
                return ModelResponse(
                    ModelMessage("assistant", "calling"),
                    (ToolCall("call-1", "weather", {"city": "Tbilisi"}),),
                )

        provider = Provider()

        def request(state, event, definitions):
            binding = current["binding"]
            resolved = template.resolve(artifacts={"policy": binding})
            return ModelRequest(
                (ModelMessage("user", "weather"),),
                definitions,
                artifacts=(binding.ref,),
                instruction=resolved,
            )

        agent, _, runtime, Started = build_runtime_agent(
            request, provider, tools, artifacts
        )
        history = drive(agent, runtime, Started)
        self.assertEqual(history[-1].event.event_type, "RunCompleted")
        self.assertEqual(len(provider.instructions), 2)
        self.assertEqual(provider.instructions[0], provider.instructions[1])
        self.assertEqual(provider.instructions[1].artifact_refs, (v1,))
        self.assertEqual(provider.instructions[1].text, "Use Policy one")


if __name__ == "__main__":
    unittest.main()
