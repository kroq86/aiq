import asyncio
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aiq import (
    AgentDefinition,
    CausalEdge,
    DanglingCausation,
    Event,
    EventEnvelope,
    EffectContext,
    EffectRegistry,
    DurableDispatcher,
    DurableEffectDispatcher,
    RunNotFoundError,
    SQLiteEventStore,
    TraceService,
    build_causal_trace,
    effect_request,
    run_stream_id,
    trace_to_json,
)


def run(coro):
    return asyncio.run(coro)


# --- Pure, no-I/O tests against build_causal_trace with hand-built envelopes ---


def _envelope(
    *,
    stream_id: str,
    stream_version: int,
    global_position: int,
    event: Event,
) -> EventEnvelope:
    return EventEnvelope(
        stream_id=stream_id,
        stream_version=stream_version,
        global_position=global_position,
        event=event,
        created_at=datetime.now(timezone.utc),
    )


def _no_reducer_agent(
    *, terminal_event_types=(), terminal_status_by_event_type=None
) -> AgentDefinition[None]:
    return AgentDefinition(
        "synthetic-agent",
        initial_state=lambda: None,
        terminal_event_types=terminal_event_types,
        terminal_status_by_event_type=terminal_status_by_event_type,
    )


class PureCausalTraceTests(unittest.TestCase):
    def test_causal_edges_follow_causation_id_not_adjacency(self) -> None:
        event_a = Event("A", {})
        event_b = Event("B", {})  # no causation_id: a root, sits *between* A and C
        event_c = Event(
            "C",
            {},
            {"causation_id": str(event_a.event_id)},  # points past B, to A
        )
        history = [
            _envelope(stream_id="s", stream_version=0, global_position=0, event=event_a),
            _envelope(stream_id="s", stream_version=1, global_position=1, event=event_b),
            _envelope(stream_id="s", stream_version=2, global_position=2, event=event_c),
        ]

        trace = build_causal_trace(
            agent_name="synthetic-agent",
            run_id="run-1",
            agent=_no_reducer_agent(),
            history=history,
        )

        self.assertEqual(
            trace.edges,
            (CausalEdge(cause_event_id=str(event_a.event_id), effect_event_id=str(event_c.event_id)),),
        )
        # Canonical stream order is preserved regardless of causal structure.
        self.assertEqual(
            [trace_event.event_type for trace_event in trace.events],
            ["A", "B", "C"],
        )

    def test_root_detection(self) -> None:
        event_a = Event("A", {})
        event_b = Event("B", {})
        event_c = Event("C", {}, {"causation_id": str(event_a.event_id)})
        history = [
            _envelope(stream_id="s", stream_version=0, global_position=0, event=event_a),
            _envelope(stream_id="s", stream_version=1, global_position=1, event=event_b),
            _envelope(stream_id="s", stream_version=2, global_position=2, event=event_c),
        ]

        trace = build_causal_trace(
            agent_name="synthetic-agent",
            run_id="run-1",
            agent=_no_reducer_agent(),
            history=history,
        )

        self.assertEqual(
            set(trace.roots),
            {str(event_a.event_id), str(event_b.event_id)},
        )
        self.assertNotIn(str(event_c.event_id), trace.roots)

    def test_missing_causation_target_does_not_crash_and_is_explicit(self) -> None:
        missing_id = uuid4()
        event_a = Event("A", {}, {"causation_id": str(missing_id)})
        history = [
            _envelope(stream_id="s", stream_version=0, global_position=0, event=event_a),
        ]

        trace = build_causal_trace(
            agent_name="synthetic-agent",
            run_id="run-1",
            agent=_no_reducer_agent(),
            history=history,
        )

        # The event itself is preserved untouched.
        self.assertEqual([e.event_type for e in trace.events], ["A"])
        # It is not silently treated as a root...
        self.assertNotIn(str(event_a.event_id), trace.roots)
        # ...nor does it produce a phantom edge...
        self.assertEqual(trace.edges, ())
        # ...instead the missing reference is explicit.
        self.assertEqual(
            trace.dangling_causation,
            (DanglingCausation(event_id=str(event_a.event_id), missing_causation_id=str(missing_id)),),
        )

    def test_terminal_status_reflects_agents_terminal_event_types(self) -> None:
        event_a = Event("UserMessageAdded", {})
        event_b = Event("RunCompleted", {}, {"causation_id": str(event_a.event_id)})
        history = [
            _envelope(stream_id="s", stream_version=0, global_position=0, event=event_a),
            _envelope(stream_id="s", stream_version=1, global_position=1, event=event_b),
        ]

        trace = build_causal_trace(
            agent_name="synthetic-agent",
            run_id="run-1",
            agent=_no_reducer_agent(terminal_event_types={"RunCompleted", "RunFailed"}),
            history=history,
        )

        self.assertTrue(trace.terminal)
        self.assertEqual(trace.terminal_event_type, "RunCompleted")
        self.assertEqual(trace.latest_stream_version, 1)

    def test_terminal_status_honors_a_custom_status_mapping(self) -> None:
        event_a = Event("UserMessageAdded", {})
        event_b = Event("ToolGaveUp", {}, {"causation_id": str(event_a.event_id)})
        history = [
            _envelope(stream_id="s", stream_version=0, global_position=0, event=event_a),
            _envelope(stream_id="s", stream_version=1, global_position=1, event=event_b),
        ]

        trace = build_causal_trace(
            agent_name="synthetic-agent",
            run_id="run-1",
            agent=_no_reducer_agent(
                terminal_event_types={"ToolGaveUp"},
                terminal_status_by_event_type={"ToolGaveUp": "abandoned"},
            ),
            history=history,
        )

        self.assertEqual(trace.terminal_event_type, "ToolGaveUp")
        self.assertEqual(trace.terminal_status, "abandoned")


# --- TraceService tests: real SQLite I/O, real dispatchers ---


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


def build_chat_agent(name: str = "energy-assistant") -> AgentDefinition[ChatState]:
    agent = AgentDefinition(
        name,
        initial_state=ChatState,
        terminal_event_types={"RunCompleted"},
    )

    @agent.reducer
    def evolve(state: ChatState, event: Event) -> ChatState:
        if event.event_type == "UserMessageAdded":
            return replace(state, messages=state.messages + (str(event.data["text"]),))
        if event.event_type == "ToolCallSucceeded":
            return replace(state, messages=state.messages + ("tool-result",))
        if event.event_type == "AnswerProduced":
            return replace(state, answer=str(event.data["text"]))
        if event.event_type == "RunCompleted":
            return replace(state, completed=True)
        return state

    @agent.react("UserMessageAdded")
    def request_model(event: Event, state: ChatState):
        return [
            effect_request(
                "ModelCallRequested",
                {"call_id": "m1"},
                {"causation_id": str(event.event_id)},
            )
        ]

    @agent.react("ModelCallSucceeded")
    def interpret_model(event: Event, state: ChatState):
        response = event.data["response"]
        if response["type"] == "tool_call":
            tool_call = response["tool_call"]
            return [
                effect_request(
                    "ToolCallRequested",
                    {
                        "call_id": tool_call["id"],
                        "tool": tool_call["name"],
                        "arguments": tool_call["arguments"],
                    },
                    {"causation_id": str(event.event_id)},
                )
            ]
        return [
            Event("AnswerProduced", {"text": response["text"]}, {"causation_id": str(event.event_id)}),
            Event("RunCompleted", {}, {"causation_id": str(event.event_id)}),
        ]

    @agent.react("ToolCallSucceeded")
    def continue_after_tool(event: Event, state: ChatState):
        return [
            effect_request(
                "ModelCallRequested",
                {"call_id": "m2"},
                {"causation_id": str(event.event_id)},
            )
        ]

    return agent


def build_effects(*, stamp_operation_id: bool) -> EffectRegistry[ChatState]:
    """`stamp_operation_id` models an application that chooses to record its
    stable operation_id (docs/effects.md: operation_id = str(request.event_id))
    back onto the result event's metadata. AIQ itself does not require or
    invent this -- trace.py only surfaces it if it's already there."""
    effects = EffectRegistry[ChatState]()

    @effects.effect("ModelCallRequested")
    async def call_model(event: Event, state: ChatState, context: EffectContext):
        metadata = {"causation_id": str(event.event_id)}
        if stamp_operation_id:
            metadata["operation_id"] = str(event.event_id)
        if "tool-result" not in state.messages:
            response = {
                "type": "tool_call",
                "tool_call": {
                    "id": "call-1",
                    "name": "get_well_pressure",
                    "arguments": {"well_id": "A-17"},
                },
            }
        else:
            response = {"type": "answer", "text": "done"}
        return [Event("ModelCallSucceeded", {"response": response}, metadata)]

    @effects.effect("ToolCallRequested")
    async def call_tool(event: Event, state: ChatState, context: EffectContext):
        metadata = {"causation_id": str(event.event_id)}
        if stamp_operation_id:
            metadata["operation_id"] = str(event.event_id)
        return [Event("ToolCallSucceeded", {"result": {"pressure": 1.0}}, metadata)]

    return effects


async def _run_chat_agent_to_completion(
    store,
    *,
    agent: AgentDefinition[ChatState],
    subscription_prefix: str,
    stamp_operation_id: bool,
    run_id: str,
    initial_stream_id: str,
) -> None:
    await store.append(
        initial_stream_id,
        -1,
        [Event("UserMessageAdded", {"text": "Pressure for A-17"})],
    )
    reactions = DurableDispatcher(
        agent=agent,
        store=store,
        subscription_name=f"{subscription_prefix}:reactions",
    )
    effects = DurableEffectDispatcher(
        agent=agent,
        store=store,
        effects=build_effects(stamp_operation_id=stamp_operation_id),
        context=EffectContext({}),
        subscription_name=f"{subscription_prefix}:effects",
    )
    for _ in range(30):
        reaction_progress = await reactions.run_once()
        effect_progress = await effects.run_once()
        if not reaction_progress and not effect_progress:
            return
    raise AssertionError("workers did not reach a stable checkpoint")


class TraceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "events.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_export_reference_chat_flow_orders_events_canonically(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            agent = build_chat_agent()
            run_id = "run-1"
            await _run_chat_agent_to_completion(
                store,
                agent=agent,
                subscription_prefix="energy-assistant",
                stamp_operation_id=False,
                run_id=run_id,
                initial_stream_id=run_stream_id("energy-assistant", run_id),
            )

            service = TraceService(store=store, agents={"energy-assistant": agent})
            trace = await service.export("energy-assistant", run_id)

            self.assertEqual(
                [trace_event.event_type for trace_event in trace.events],
                [
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "ToolCallRequested",
                    "ToolCallSucceeded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )
            self.assertTrue(trace.terminal)
            self.assertEqual(trace.terminal_event_type, "RunCompleted")
            self.assertEqual(trace.agent_name, "energy-assistant")
            self.assertEqual(trace.run_id, run_id)

        run(scenario())

    def test_operation_id_from_effect_request_events_is_preserved(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            agent = build_chat_agent()
            run_id = "run-1"
            await _run_chat_agent_to_completion(
                store,
                agent=agent,
                subscription_prefix="energy-assistant",
                stamp_operation_id=True,
                run_id=run_id,
                initial_stream_id=run_stream_id("energy-assistant", run_id),
            )

            service = TraceService(store=store, agents={"energy-assistant": agent})
            trace = await service.export("energy-assistant", run_id)

            model_requests = [e for e in trace.events if e.event_type == "ModelCallRequested"]
            model_results = [e for e in trace.events if e.event_type == "ModelCallSucceeded"]
            self.assertEqual(len(model_requests), 2)
            self.assertEqual(len(model_results), 2)
            for request_event, result_event in zip(model_requests, model_results):
                self.assertEqual(result_event.operation_id, request_event.event_id)

        run(scenario())

    def test_stream_version_and_global_position_are_distinct(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            # Push global_position ahead of stream_version for our run by
            # writing to an unrelated stream first.
            await store.append("filler-stream", -1, [Event("Filler", {}) for _ in range(3)])

            agent = build_chat_agent()
            run_id = "run-1"
            await _run_chat_agent_to_completion(
                store,
                agent=agent,
                subscription_prefix="energy-assistant",
                stamp_operation_id=False,
                run_id=run_id,
                initial_stream_id=run_stream_id("energy-assistant", run_id),
            )

            service = TraceService(store=store, agents={"energy-assistant": agent})
            trace = await service.export("energy-assistant", run_id)

            first_event = trace.events[0]
            self.assertEqual(first_event.stream_version, 0)
            self.assertNotEqual(first_event.global_position, first_event.stream_version)
            self.assertGreater(first_event.global_position, 0)
            # And they both keep tracking their own axis across the run.
            self.assertEqual(
                [e.stream_version for e in trace.events],
                list(range(len(trace.events))),
            )
            self.assertEqual(
                [e.global_position for e in trace.events],
                sorted(e.global_position for e in trace.events),
            )

        run(scenario())

    def test_export_unknown_run_raises_run_not_found(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            agent = build_chat_agent()
            service = TraceService(store=store, agents={"energy-assistant": agent})
            with self.assertRaises(RunNotFoundError):
                await service.export("energy-assistant", "does-not-exist")

        run(scenario())

    def test_run_from_agent_b_cannot_be_exported_through_agent_a(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            agent_a = build_chat_agent("agent-a")
            agent_b = build_chat_agent("agent-b")
            run_id = "run-1"
            await _run_chat_agent_to_completion(
                store,
                agent=agent_a,
                subscription_prefix="agent-a",
                stamp_operation_id=False,
                run_id=run_id,
                initial_stream_id=run_stream_id("agent-a", run_id),
            )

            service = TraceService(
                store=store,
                agents={"agent-a": agent_a, "agent-b": agent_b},
            )
            # Sanity: it does exist under its real agent.
            await service.export("agent-a", run_id)

            with self.assertRaises(RunNotFoundError):
                await service.export("agent-b", run_id)

        run(scenario())

    def test_reopen_durability_produces_an_equal_trace(self) -> None:
        async def scenario() -> None:
            run_id = "run-1"
            first_store = await SQLiteEventStore.open(self.path)
            agent = build_chat_agent()
            await _run_chat_agent_to_completion(
                first_store,
                agent=agent,
                subscription_prefix="energy-assistant",
                stamp_operation_id=True,
                run_id=run_id,
                initial_stream_id=run_stream_id("energy-assistant", run_id),
            )
            first_service = TraceService(store=first_store, agents={"energy-assistant": agent})
            trace_before = await first_service.export("energy-assistant", run_id)

            # Drop the first store/service entirely and rebuild everything
            # fresh from the same SQLite file, as a new process would.
            del first_store, first_service
            reopened_store = await SQLiteEventStore.open(self.path)
            reopened_agent = build_chat_agent()
            reopened_service = TraceService(
                store=reopened_store,
                agents={"energy-assistant": reopened_agent},
            )
            trace_after = await reopened_service.export("energy-assistant", run_id)

            self.assertEqual(trace_before, trace_after)
            # The canonical serialized JSON -- what an external consumer
            # actually receives -- must be exactly equal too, not just the
            # Python dataclass. created_at is read back from SQLite, not
            # regenerated, so nothing here is nondeterministic.
            self.assertEqual(trace_to_json(trace_before), trace_to_json(trace_after))

        run(scenario())


if __name__ == "__main__":
    unittest.main()
