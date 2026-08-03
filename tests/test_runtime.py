import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from agentlog import (
    AgentDefinition,
    DefinitionMismatchError,
    DuplicateEventError,
    DurableDispatcher,
    DurableEffectDispatcher,
    EffectContext,
    EffectRegistry,
    Event,
    EventEnvelope,
    InMemoryEventStore,
    SQLiteEventStore,
    effect_request,
    run_stream_id,
)


def run(coro):
    return asyncio.run(coro)


def own_all_streams(stream_id: str) -> bool:
    return True


@dataclass(frozen=True)
class AgentState:
    messages: tuple[str, ...] = ()


def build_agent(
    *,
    duplicate_event: Event | None = None,
) -> AgentDefinition[AgentState]:
    agent = AgentDefinition("assistant", initial_state=AgentState)

    @agent.reducer
    def evolve(state: AgentState, event: Event) -> AgentState:
        if event.event_type == "UserMessageAdded":
            return replace(
                state,
                messages=state.messages + (str(event.data["text"]),),
            )
        return state

    @agent.react("UserMessageAdded")
    def request_model(event: Event, state: AgentState):
        outputs = [
            effect_request(
                "ModelCallRequested",
                {
                    "message_count": len(state.messages),
                    "caused_by": str(event.event_id),
                },
            )
        ]
        if duplicate_event is not None:
            outputs.append(duplicate_event)
        return outputs

    return agent


class DurableDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "events.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_reaction_output_and_checkpoint_commit_atomically(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        run(
            store.append(
                "run-1",
                -1,
                [Event("UserMessageAdded", {"text": "hello"})],
            )
        )
        dispatcher = DurableDispatcher(
            agent=build_agent(),
            store=store,
            subscription_name="assistant:reactions",
            owns_stream=own_all_streams,
        )

        self.assertIs(run(dispatcher.run_once()), True)
        self.assertEqual(run(store.load_checkpoint("assistant:reactions")), 1)
        history = run(store.load("run-1"))
        self.assertEqual(
            [envelope.event.event_type for envelope in history],
            ["UserMessageAdded", "ModelCallRequested"],
        )
        self.assertEqual(history[1].event.data["message_count"], 1)

        self.assertIs(run(dispatcher.run_once()), True)
        self.assertIs(run(dispatcher.run_once()), False)
        history_after_repeat = run(store.load("run-1"))
        self.assertEqual(
            [envelope.event.event_type for envelope in history_after_repeat].count(
                "ModelCallRequested"
            ),
            1,
        )

    def test_failed_output_batch_keeps_events_and_checkpoint_unchanged(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        duplicate = Event("AlreadyStored", {})
        run(
            store.append(
                "run-1",
                -1,
                [Event("UserMessageAdded", {"text": "hello"})],
            )
        )
        run(store.append("other-run", -1, [duplicate]))
        dispatcher = DurableDispatcher(
            agent=build_agent(duplicate_event=duplicate),
            store=store,
            subscription_name="assistant:reactions",
            owns_stream=own_all_streams,
        )

        with self.assertRaises(DuplicateEventError):
            run(dispatcher.run_once())

        self.assertEqual(run(store.load_checkpoint("assistant:reactions")), 0)
        self.assertEqual(
            [item.event.event_type for item in run(store.load("run-1"))],
            ["UserMessageAdded"],
        )

    def test_definition_rejects_async_and_duplicate_handlers(self) -> None:
        agent = AgentDefinition("assistant", initial_state=AgentState)

        async def async_reducer(state, event):
            return state

        with self.assertRaisesRegex(TypeError, "reducer must be synchronous"):
            agent.reducer(async_reducer)

        @agent.reducer
        def reducer(state, event):
            return state

        with self.assertRaisesRegex(ValueError, "already has a reducer"):
            agent.reducer(reducer)

        async def async_reaction(event, state):
            return ()

        with self.assertRaisesRegex(TypeError, "reaction must be synchronous"):
            agent.react("Input")(async_reaction)

    def test_full_and_bounded_replay_are_separate_explicit_operations(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        agent = build_agent()
        run(
            store.append(
                "run-1",
                -1,
                [
                    Event("UserMessageAdded", {"text": "one"}),
                    Event("UserMessageAdded", {"text": "two"}),
                ],
            )
        )
        history = run(store.load("run-1"))

        self.assertEqual(
            agent.rebuild(history).messages,
            ("one", "two"),
        )
        self.assertEqual(
            agent.rebuild_through(history, through_version=0).messages,
            ("one",),
        )
        self.assertEqual(
            agent.rebuild_through(history, through_version=-1).messages,
            (),
        )

    def test_version_conflict_retries_commit_without_reexecuting_reaction(
        self,
    ) -> None:
        """A concurrent writer (e.g. an HTTP command) appending to the same
        stream between this dispatcher's read and its commit must not
        cause the reaction to be re-evaluated -- only the atomic commit
        itself is retried, reusing the already-produced output batch."""
        call_count = 0

        agent = AgentDefinition("assistant", initial_state=AgentState)

        @agent.reducer
        def evolve(state: AgentState, event: Event) -> AgentState:
            return state

        @agent.react("UserMessageAdded")
        def request_model(event: Event, state: AgentState):
            nonlocal call_count
            call_count += 1
            return [Event("ModelCallRequested", {})]

        store = InMemoryEventStore()
        run(store.append("run-1", -1, [Event("UserMessageAdded", {"text": "hi"})]))

        class RacingStore:
            def __init__(self, inner):
                self._inner = inner
                self._load_calls = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def load(self, stream_id, **kwargs):
                self._load_calls += 1
                # 1st load() = run_once()'s initial history read (before
                # evaluating the reaction); 2nd load() =
                # _commit_outputs_with_retry's fresh read right before
                # committing -- inject the race there, i.e. after the
                # reaction already produced its output.
                if self._load_calls == 2:
                    version = await self._inner.current_version(stream_id)
                    await self._inner.append(
                        stream_id, version, [Event("UnrelatedConcurrentEvent", {})]
                    )
                return await self._inner.load(stream_id, **kwargs)

        dispatcher = DurableDispatcher(
            agent=agent,
            store=RacingStore(store),
            subscription_name="reactions",
            owns_stream=own_all_streams,
        )

        advanced = run(dispatcher.run_once())
        self.assertTrue(advanced)
        self.assertEqual(call_count, 1)

        history = run(store.load("run-1"))
        self.assertEqual(
            [envelope.event.event_type for envelope in history],
            ["UserMessageAdded", "UnrelatedConcurrentEvent", "ModelCallRequested"],
        )

    def test_assert_definition_matches_raises_on_mismatch_and_is_a_noop_otherwise(
        self,
    ) -> None:
        def envelope(event: Event) -> EventEnvelope:
            return EventEnvelope(
                stream_id="assistant:run-1",
                stream_version=0,
                global_position=0,
                event=event,
                created_at=datetime.now(timezone.utc),
            )

        versioned_agent = AgentDefinition(
            "assistant", initial_state=AgentState, definition_version="v2"
        )

        # No RunCreated at all (e.g. the non-HTTP flow) -- nothing to
        # compare against.
        versioned_agent.assert_definition_matches(
            [envelope(Event("UserMessageAdded", {}))]
        )

        # RunCreated with no recorded version -- opt-in, no-op.
        versioned_agent.assert_definition_matches(
            [
                envelope(
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": None},
                    )
                )
            ]
        )

        # Matching (agent_name, definition_version) -- fine.
        versioned_agent.assert_definition_matches(
            [
                envelope(
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "v2"},
                    )
                )
            ]
        )

        # Mismatched version -- raises.
        with self.assertRaises(DefinitionMismatchError):
            versioned_agent.assert_definition_matches(
                [
                    envelope(
                        Event(
                            "RunCreated",
                            {"agent": "assistant", "definition_version": "v1"},
                        )
                    )
                ]
            )

        # An unversioned definition opts out of the check entirely, even if
        # the run's history has a recorded version.
        unversioned_agent = AgentDefinition("assistant", initial_state=AgentState)
        unversioned_agent.assert_definition_matches(
            [
                envelope(
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "v1"},
                    )
                )
            ]
        )

    def test_dispatcher_skips_definition_mismatched_stream_without_blocking_others(
        self,
    ) -> None:
        """DefinitionMismatchError must not be worker-fatal: Mismatch(r1)
        must not imply Unavailable(r2) for a different run r2. The
        dispatcher skips just the mismatched stream's pending event
        (advancing its checkpoint, same treatment as a foreign/not-owned
        stream) and continues on to the next one."""
        store = InMemoryEventStore()
        run(
            store.append(
                "assistant:run-old",
                -1,
                [
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "v1"},
                    ),
                    Event("UserMessageAdded", {"text": "old"}),
                ],
            )
        )
        run(
            store.append(
                "assistant:run-new",
                -1,
                [
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "v2"},
                    ),
                    Event("UserMessageAdded", {"text": "new"}),
                ],
            )
        )

        agent = AgentDefinition(
            "assistant", initial_state=AgentState, definition_version="v2"
        )

        @agent.reducer
        def evolve(state: AgentState, event: Event) -> AgentState:
            if event.event_type == "UserMessageAdded":
                return replace(
                    state, messages=state.messages + (str(event.data["text"]),)
                )
            return state

        @agent.react("UserMessageAdded")
        def request_model(event: Event, state: AgentState):
            return [Event("ModelCallRequested", {})]

        dispatcher = DurableDispatcher(
            agent=agent,
            store=store,
            subscription_name="reactions",
            owns_stream=own_all_streams,
        )

        for _ in range(10):
            if not run(dispatcher.run_once()):
                break

        old_history = run(store.load("assistant:run-old"))
        new_history = run(store.load("assistant:run-new"))
        self.assertEqual(
            [envelope.event.event_type for envelope in old_history],
            ["RunCreated", "UserMessageAdded"],
        )
        self.assertEqual(
            [envelope.event.event_type for envelope in new_history],
            ["RunCreated", "UserMessageAdded", "ModelCallRequested"],
        )


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def respond(
        self,
        messages: tuple[str, ...],
        operation_id: str,
    ) -> dict:
        self.calls.append((messages, operation_id))
        if len(self.calls) == 1:
            return {
                "type": "tool_call",
                "tool_call": {
                    "id": "tool-call-1",
                    "name": "get_well_pressure",
                    "arguments": {"well_id": "A-17", "hours": 24},
                },
            }
        return {
            "type": "answer",
            "text": f"Pressure received after {len(messages)} messages",
        }


class FakeMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    async def call(
        self,
        tool: str,
        arguments: dict,
        operation_id: str,
    ) -> dict:
        self.calls.append((tool, arguments, operation_id))
        return {"well_id": "A-17", "pressure": 152.4}


def build_chat_agent() -> AgentDefinition[ChatState]:
    agent = AgentDefinition(
        "energy-assistant",
        initial_state=ChatState,
        terminal_event_types={
            "RunCompleted",
            "RunFailed",
            "RunCancelled",
        },
    )

    @agent.reducer
    def evolve(state: ChatState, event: Event) -> ChatState:
        if event.event_type == "UserMessageAdded":
            return replace(
                state,
                messages=state.messages + (f"user:{event.data['text']}",),
            )
        if event.event_type == "ToolCallSucceeded":
            return replace(
                state,
                messages=state.messages + (f"tool:{event.data['result']}",),
            )
        if event.event_type == "AnswerProduced":
            return replace(state, answer=str(event.data["text"]))
        if event.event_type == "RunCompleted":
            return replace(state, completed=True)
        return state

    @agent.react("UserMessageAdded")
    def request_initial_model(event: Event, state: ChatState):
        return [
            effect_request(
                "ModelCallRequested",
                {"call_id": "model-call-1"},
                {"causation_id": str(event.event_id)},
            )
        ]

    @agent.react("ModelCallSucceeded")
    def interpret_model(event: Event, state: ChatState):
        response = event.data["response"]
        if response["type"] not in {"tool_call", "answer"}:
            return [
                Event(
                    "ModelOutputRejected",
                    {
                        "reason": "unsupported response type",
                        "response_type": response["type"],
                    },
                ),
                Event(
                    "AnswerProduced",
                    {"text": "The model returned an unsupported response."},
                ),
                Event("RunCompleted", {}),
            ]
        if response["type"] == "tool_call":
            tool_call = response["tool_call"]
            if tool_call["name"] != "get_well_pressure":
                return [
                    Event(
                        "ToolCallRejected",
                        {
                            "tool": tool_call["name"],
                            "reason": "unknown tool",
                        },
                    ),
                    Event(
                        "AnswerProduced",
                        {"text": "The requested tool is not available."},
                    ),
                    Event("RunCompleted", {}),
                ]
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
            Event(
                "AnswerProduced",
                {"text": response["text"]},
                {"causation_id": str(event.event_id)},
            ),
            Event(
                "RunCompleted",
                {},
                {"causation_id": str(event.event_id)},
            ),
        ]

    @agent.react("ToolCallSucceeded")
    def continue_after_tool(event: Event, state: ChatState):
        return [
            effect_request(
                "ModelCallRequested",
                {"call_id": "model-call-2"},
                {"causation_id": str(event.event_id)},
            )
        ]

    return agent


class DurableEffectDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "events.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_chat_llm_mcp_answer_vertical_flow(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        agent = build_chat_agent()
        llm = FakeLLM()
        mcp = FakeMCP()
        effects = EffectRegistry[ChatState]()

        @effects.effect("ModelCallRequested")
        async def call_model(event: Event, state: ChatState, context: EffectContext):
            response = await context.require("llm").respond(
                state.messages,
                operation_id=str(event.event_id),
            )
            return [
                Event(
                    "ModelCallSucceeded",
                    {
                        "call_id": event.data["call_id"],
                        "response": response,
                    },
                    {"causation_id": str(event.event_id)},
                )
            ]

        @effects.effect("ToolCallRequested")
        async def call_tool(event: Event, state: ChatState, context: EffectContext):
            result = await context.require("mcp").call(
                tool=event.data["tool"],
                arguments=dict(event.data["arguments"]),
                operation_id=str(event.event_id),
            )
            return [
                Event(
                    "ToolCallSucceeded",
                    {
                        "call_id": event.data["call_id"],
                        "tool": event.data["tool"],
                        "result": result,
                    },
                    {"causation_id": str(event.event_id)},
                )
            ]

        reactions = DurableDispatcher(
            agent=agent,
            store=store,
            subscription_name="energy-assistant:reactions",
            owns_stream=own_all_streams,
        )
        effect_worker = DurableEffectDispatcher(
            agent=agent,
            store=store,
            effects=effects,
            context=EffectContext({"llm": llm, "mcp": mcp}),
            subscription_name="energy-assistant:effects",
            owns_stream=own_all_streams,
        )
        run(
            store.append(
                "run-1",
                -1,
                [Event("UserMessageAdded", {"text": "Pressure for A-17"})],
            )
        )

        async def catch_up() -> None:
            for _ in range(30):
                reaction_progress = await reactions.run_once()
                effect_progress = await effect_worker.run_once()
                if not reaction_progress and not effect_progress:
                    return
            self.fail("workers did not reach a stable checkpoint")

        run(catch_up())
        history = run(store.load("run-1"))

        self.assertEqual(
            [item.event.event_type for item in history],
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
        self.assertEqual(len(llm.calls), 2)
        model_request_ids = [
            str(item.event.event_id)
            for item in history
            if item.event.event_type == "ModelCallRequested"
        ]
        self.assertEqual(
            [operation_id for _, operation_id in llm.calls],
            model_request_ids,
        )
        tool_request = next(
            item.event
            for item in history
            if item.event.event_type == "ToolCallRequested"
        )
        self.assertEqual(
            mcp.calls,
            [
                (
                    "get_well_pressure",
                    {"well_id": "A-17", "hours": 24},
                    str(tool_request.event_id),
                )
            ],
        )
        state = agent.rebuild(history)
        self.assertIs(state.completed, True)
        self.assertIn("Pressure received", state.answer)

        llm_call_count = len(llm.calls)
        mcp_call_count = len(mcp.calls)
        run(catch_up())
        self.assertEqual(len(llm.calls), llm_call_count)
        self.assertEqual(len(mcp.calls), mcp_call_count)

    def test_uncommitted_effect_failure_is_retried_at_least_once(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        agent = build_chat_agent()
        effects = EffectRegistry[ChatState]()
        attempts = 0

        @effects.effect("ModelCallRequested")
        async def flaky_effect(event: Event, state: ChatState, context: EffectContext):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("response outcome is unknown")
            return [Event("ModelCallSucceeded", {"response": {"type": "answer", "text": "ok"}})]

        worker = DurableEffectDispatcher(
            agent=agent,
            store=store,
            effects=effects,
            context=EffectContext({}),
            subscription_name="effects",
            owns_stream=own_all_streams,
        )
        run(
            store.append(
                "run-1",
                -1,
                [effect_request("ModelCallRequested", {"call_id": "call-1"})],
            )
        )

        with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
            run(worker.run_once())

        self.assertEqual(run(store.load_checkpoint("effects")), 0)
        self.assertEqual(len(run(store.load("run-1"))), 1)
        self.assertIs(run(worker.run_once()), True)
        self.assertEqual(attempts, 2)
        self.assertEqual(
            [item.event.event_type for item in run(store.load("run-1"))],
            ["ModelCallRequested", "ModelCallSucceeded"],
        )

    def test_effect_dispatch_reloads_history_once_before_committing(
        self,
    ) -> None:
        """Two `load()` calls, not one: the initial dispatch read (before
        invoking the handler) and one fresh reload inside
        `_commit_outputs_with_retry` right before the commit, used to
        re-check terminal status against a race (see
        test_version_conflict_retries_commit_without_reexecuting_effect
        and the terminal-absorption tests) -- `current_version()` is no
        longer called at all, since the freshly reloaded history already
        carries the current stream version."""

        class CountingStore(InMemoryEventStore):
            def __init__(self) -> None:
                super().__init__()
                self.load_calls = 0
                self.current_version_calls = 0

            async def load(self, stream_id: str, *, after_version: int = -1):
                self.load_calls += 1
                return await super().load(
                    stream_id,
                    after_version=after_version,
                )

            async def current_version(self, stream_id: str) -> int:
                self.current_version_calls += 1
                return await super().current_version(stream_id)

        store = CountingStore()
        agent = build_chat_agent()
        effects = EffectRegistry[ChatState]()

        @effects.effect("ModelCallRequested")
        async def call_model(event: Event, state: ChatState, context: EffectContext):
            return [
                Event(
                    "ModelCallSucceeded",
                    {"response": {"type": "answer", "text": "ok"}},
                )
            ]

        run(
            store.append(
                "run-1",
                -1,
                [effect_request("ModelCallRequested", {"call_id": "call-1"})],
            )
        )
        worker = DurableEffectDispatcher(
            agent=agent,
            store=store,
            effects=effects,
            context=EffectContext({}),
            subscription_name="effects",
            owns_stream=own_all_streams,
        )

        self.assertIs(run(worker.run_once()), True)
        self.assertEqual(store.load_calls, 2)
        self.assertEqual(store.current_version_calls, 0)

    def test_result_and_checkpoint_roll_back_then_retry_with_same_operation_id(
        self,
    ) -> None:
        store = run(SQLiteEventStore.open(self.path))
        agent = build_chat_agent()
        effects = EffectRegistry[ChatState]()
        operation_ids: list[str] = []

        @effects.effect("ModelCallRequested")
        async def call_model(event: Event, state: ChatState, context: EffectContext):
            operation_ids.append(str(event.event_id))
            return [
                Event(
                    "ModelCallSucceeded",
                    {
                        "call_id": event.data["call_id"],
                        "response": {"type": "answer", "text": "ok"},
                    },
                )
            ]

        request = effect_request("ModelCallRequested", {"call_id": "call-1"})
        run(store.append("run-1", -1, [request]))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                CREATE TRIGGER fail_effect_checkpoint
                BEFORE INSERT ON subscription_checkpoints
                BEGIN
                    SELECT RAISE(ABORT, 'injected checkpoint failure');
                END;
                """
            )

        worker = DurableEffectDispatcher(
            agent=agent,
            store=store,
            effects=effects,
            context=EffectContext({}),
            subscription_name="effects",
            owns_stream=own_all_streams,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "checkpoint failure"):
            run(worker.run_once())

        self.assertEqual(run(store.load_checkpoint("effects")), 0)
        self.assertEqual(
            [item.event.event_type for item in run(store.load("run-1"))],
            ["ModelCallRequested"],
        )

        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TRIGGER fail_effect_checkpoint")
            connection.commit()

        self.assertIs(run(worker.run_once()), True)
        self.assertEqual(operation_ids, [str(request.event_id), str(request.event_id)])
        self.assertEqual(
            [item.event.event_type for item in run(store.load("run-1"))],
            ["ModelCallRequested", "ModelCallSucceeded"],
        )
        self.assertEqual(run(store.load_checkpoint("effects")), 1)

    def test_domain_failure_event_advances_effect_checkpoint(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        agent = build_chat_agent()
        effects = EffectRegistry[ChatState]()

        @effects.effect("ToolCallRequested")
        async def call_tool(event: Event, state: ChatState, context: EffectContext):
            return [
                Event(
                    "ToolCallFailed",
                    {
                        "call_id": event.data["call_id"],
                        "code": "well_not_found",
                    },
                )
            ]

        run(
            store.append(
                "run-1",
                -1,
                [effect_request("ToolCallRequested", {"call_id": "tool-1"})],
            )
        )
        worker = DurableEffectDispatcher(
            agent=agent,
            store=store,
            effects=effects,
            context=EffectContext({}),
            subscription_name="effects",
            owns_stream=own_all_streams,
        )

        self.assertIs(run(worker.run_once()), True)
        self.assertEqual(run(store.load_checkpoint("effects")), 1)
        self.assertEqual(
            [item.event.event_type for item in run(store.load("run-1"))],
            ["ToolCallRequested", "ToolCallFailed"],
        )

    def test_unknown_model_tool_is_rejected_as_terminal_domain_result(self) -> None:
        agent = build_chat_agent()
        model_result = Event(
            "ModelCallSucceeded",
            {
                "response": {
                    "type": "tool_call",
                    "tool_call": {
                        "id": "unknown-1",
                        "name": "delete_everything",
                        "arguments": {},
                    },
                }
            },
        )

        outputs = agent.evaluate_reaction(model_result, ChatState())

        self.assertEqual(
            [event.event_type for event in outputs],
            ["ToolCallRejected", "AnswerProduced", "RunCompleted"],
        )

    def test_invalid_model_result_is_rejected_without_retry_loop(self) -> None:
        agent = build_chat_agent()
        model_result = Event(
            "ModelCallSucceeded",
            {"response": {"type": "unsupported"}},
        )

        outputs = agent.evaluate_reaction(model_result, ChatState())

        self.assertEqual(
            [event.event_type for event in outputs],
            ["ModelOutputRejected", "AnswerProduced", "RunCompleted"],
        )

    def test_events_after_terminal_event_do_not_create_new_actions(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        agent = build_chat_agent()
        run(
            store.append(
                "run-1",
                -1,
                [
                    Event("RunCompleted", {}),
                    Event("UserMessageAdded", {"text": "late message"}),
                ],
            )
        )
        worker = DurableDispatcher(
            agent=agent,
            store=store,
            subscription_name="reactions",
            owns_stream=own_all_streams,
        )

        self.assertIs(run(worker.run_once()), True)
        self.assertIs(run(worker.run_once()), True)
        self.assertEqual(
            [item.event.event_type for item in run(store.load("run-1"))],
            ["RunCompleted", "UserMessageAdded"],
        )

    def test_version_conflict_retries_commit_without_reexecuting_effect(
        self,
    ) -> None:
        """External I/O must never be repeated just because the commit
        raced a concurrent writer: the effect handler runs exactly once,
        and only the atomic commit is retried against a fresh stream
        version, reusing the same produced output."""
        call_count = 0

        agent = AgentDefinition("assistant", initial_state=AgentState)

        @agent.reducer
        def evolve(state: AgentState, event: Event) -> AgentState:
            return state

        effects = EffectRegistry[AgentState]()

        @effects.effect("ModelCallRequested")
        async def call_model(event: Event, state: AgentState, context: EffectContext):
            nonlocal call_count
            call_count += 1
            return [Event("ModelCallSucceeded", {})]

        store = InMemoryEventStore()
        run(store.append("run-1", -1, [effect_request("ModelCallRequested", {})]))

        class RacingStore:
            def __init__(self, inner):
                self._inner = inner
                self._load_calls = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def load(self, stream_id, **kwargs):
                self._load_calls += 1
                # 1st load() = run_once()'s initial history_at_dispatch
                # read (before invoking the handler); 2nd load() =
                # _commit_outputs_with_retry's fresh read right before
                # committing -- inject the race there, i.e. after the
                # effect handler already computed its output.
                if self._load_calls == 2:
                    version = await self._inner.current_version(stream_id)
                    await self._inner.append(
                        stream_id, version, [Event("UnrelatedConcurrentEvent", {})]
                    )
                return await self._inner.load(stream_id, **kwargs)

        dispatcher = DurableEffectDispatcher(
            agent=agent,
            store=RacingStore(store),
            effects=effects,
            context=EffectContext({}),
            subscription_name="effects",
            owns_stream=own_all_streams,
        )

        advanced = run(dispatcher.run_once())
        self.assertTrue(advanced)
        self.assertEqual(call_count, 1)

        history = run(store.load("run-1"))
        self.assertEqual(
            [envelope.event.event_type for envelope in history],
            [
                "ModelCallRequested",
                "UnrelatedConcurrentEvent",
                "ModelCallSucceeded",
            ],
        )


class FrameworkApiSmokeTests(unittest.TestCase):
    """Minimal proof that agentlog.framework.Agent actually compiles into
    and drives the existing runtime -- not a second engine. Full framework
    behavior lives in examples/framework_chat_agent.py, exercised directly
    by hand in this task rather than duplicated into a large test suite."""

    def _build_declared_agent(self):
        from agentlog.framework import Agent

        @dataclass(frozen=True)
        class DemoState:
            messages: tuple[str, ...] = ()
            done: bool = False

        agent = Agent(name="framework-smoke", initial_state=DemoState())

        @agent.event
        @dataclass(frozen=True)
        class Added:
            text: str

        @agent.event
        @dataclass(frozen=True)
        class Done:
            pass

        @agent.reduce(Added)
        def on_added(state, event):
            return replace(state, messages=state.messages + (event.text,))

        @agent.reduce(Done)
        def on_done(state, event):
            return replace(state, done=True)

        @agent.react(Added)
        def request_done(state, event):
            return Done()

        return agent, Added

    def test_build_runtime_drives_a_real_run_through_the_existing_core(self) -> None:
        agent, Added = self._build_declared_agent()
        runtime = agent.build_runtime()

        async def scenario():
            # handle_command is exercised separately below; construct the
            # root event directly here to keep this test focused on
            # build_runtime() actually driving the existing dispatcher.
            # DurableDispatcher's default owns_stream predicate is
            # agent_owns_stream(agent.name, stream_id), so the stream id
            # must actually belong to this agent's namespace.
            store = InMemoryEventStore()
            stream_id = run_stream_id(agent.name, "run-1")
            await store.append(stream_id, -1, [Event("Added", {"text": "hi"})])
            reactions = DurableDispatcher(
                agent=runtime.agent, store=store, subscription_name="reactions"
            )
            for _ in range(10):
                if not await reactions.run_once():
                    break
            history = await store.load(stream_id)
            return [e.event.event_type for e in history], runtime.agent.rebuild(history)

        event_types, final_state = run(scenario())
        self.assertEqual(event_types, ["Added", "Done"])
        self.assertEqual(final_state.messages, ("hi",))
        self.assertTrue(final_state.done)

    def test_handle_command_produces_raw_events(self) -> None:
        agent, Added = self._build_declared_agent()

        @agent.command("add")
        def add(payload):
            return Added(text=payload)

        produced = agent.handle_command("add", "hello")
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].event_type, "Added")
        self.assertEqual(produced[0].data["text"], "hello")

    def test_duplicate_reducer_for_same_event_type_is_rejected(self) -> None:
        agent, Added = self._build_declared_agent()
        with self.assertRaises(ValueError):

            @agent.reduce(Added)
            def another(state, event):
                return state

    def test_reduce_for_unregistered_event_type_is_rejected(self) -> None:
        from agentlog.framework import Agent

        agent = Agent(name="framework-smoke-2", initial_state=lambda: None)

        @dataclass(frozen=True)
        class NotRegistered:
            pass

        with self.assertRaises(ValueError):

            @agent.reduce(NotRegistered)
            def handler(state, event):
                return state

    def test_sync_function_registered_as_effect_is_rejected(self) -> None:
        from agentlog.framework import Agent

        agent = Agent(name="framework-smoke-3", initial_state=lambda: None)

        @agent.event
        @dataclass(frozen=True)
        class Requested:
            pass

        with self.assertRaises(TypeError):

            @agent.effect(Requested)
            def not_async(effect, context):
                return None

    def test_terminal_stops_reactions_after_marked_event(self) -> None:
        agent, Added = self._build_declared_agent()
        Done = agent.event_type("Done")
        agent.terminal(Done, status="completed")
        runtime = agent.build_runtime()

        async def scenario():
            store = InMemoryEventStore()
            stream_id = run_stream_id(agent.name, "run-1")
            await store.append(stream_id, -1, [Event("Added", {"text": "hi"})])
            dispatcher = DurableDispatcher(
                agent=runtime.agent, store=store, subscription_name="reactions"
            )
            for _ in range(10):
                if not await dispatcher.run_once():
                    break
            history = await store.load(stream_id)
            # Run is already terminal (Done reached) -- a further event must
            # not trigger any additional reaction output.
            await store.append(
                stream_id, len(history) - 1, [Event("Added", {"text": "late"})]
            )
            for _ in range(10):
                if not await dispatcher.run_once():
                    break
            return await store.load(stream_id)

        history = run(scenario())
        event_types = [envelope.event.event_type for envelope in history]
        self.assertEqual(event_types, ["Added", "Done", "Added"])
        self.assertTrue(
            runtime.agent.is_terminal(
                history, through_version=history[-1].stream_version
            )
        )

    def test_command_rejected_is_caught_and_becomes_a_domain_event(self) -> None:
        from agentlog.framework import CommandRejected

        agent, Added = self._build_declared_agent()

        @agent.command("add")
        def add(payload):
            if not payload:
                raise CommandRejected("text must not be empty")
            return Added(text=payload)

        produced = agent.handle_command("add", "")
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].event_type, "CommandRejected")
        self.assertEqual(produced[0].data["reason"], "text must not be empty")

    def test_effect_failed_is_caught_and_commits_atomically(self) -> None:
        from agentlog.framework import Agent, EffectFailed

        agent = Agent(name="framework-smoke-4", initial_state=lambda: None)

        @agent.event
        @dataclass(frozen=True)
        class Requested:
            pass

        @agent.effect(Requested)
        async def call(effect, context):
            raise EffectFailed("boom")

        runtime = agent.build_runtime()

        async def scenario():
            store = InMemoryEventStore()
            stream_id = run_stream_id(agent.name, "run-1")
            await store.append(stream_id, -1, [effect_request("Requested", {})])
            dispatcher = DurableEffectDispatcher(
                agent=runtime.agent,
                store=store,
                effects=runtime.effects,
                context=runtime.context,
                subscription_name="effects",
            )
            advanced = await dispatcher.run_once()
            history = await store.load(stream_id)
            checkpoint = await store.load_checkpoint("effects")
            return advanced, history, checkpoint

        advanced, history, checkpoint = run(scenario())
        self.assertTrue(advanced)
        event_types = [envelope.event.event_type for envelope in history]
        self.assertEqual(event_types, ["Requested", "EffectFailed"])
        self.assertEqual(history[-1].event.data["reason"], "boom")
        # Checkpoint tracks progress through the *consumed* global stream
        # (the request event), not the freshly appended result -- this is
        # what proves the subscription actually advanced instead of
        # getting stuck retrying the same effect forever.
        self.assertEqual(checkpoint, history[0].global_position)

    def test_undeclared_exception_from_effect_still_propagates(self) -> None:
        from agentlog.framework import Agent

        agent = Agent(name="framework-smoke-5", initial_state=lambda: None)

        @agent.event
        @dataclass(frozen=True)
        class Requested:
            pass

        @agent.effect(Requested)
        async def call(effect, context):
            raise RuntimeError("boom")

        runtime = agent.build_runtime()

        async def scenario():
            store = InMemoryEventStore()
            stream_id = run_stream_id(agent.name, "run-1")
            await store.append(stream_id, -1, [effect_request("Requested", {})])
            dispatcher = DurableEffectDispatcher(
                agent=runtime.agent,
                store=store,
                effects=runtime.effects,
                context=runtime.context,
                subscription_name="effects",
            )
            await dispatcher.run_once()

        with self.assertRaises(RuntimeError):
            run(scenario())

    def test_resuming_a_run_from_a_fresh_agent_and_context(self) -> None:
        """No future behavior may depend on information absent from
        persisted history or explicit resources: generation 1 starts a run
        and is discarded entirely (never runs the effect); generation 2 --
        a brand new Agent, a brand new resource instance, a freshly
        reopened SQLite file -- must finish it correctly using nothing
        else. See docs/effects.md#no-hidden-operational-state."""
        from agentlog.framework import Agent

        @dataclass(frozen=True)
        class ResumeState:
            messages: tuple[str, ...] = ()
            greeting: str | None = None

        class Greeting:
            def __init__(self, text: str) -> None:
                self.text = text

        def build():
            agent = Agent(name="resume-smoke", initial_state=ResumeState())

            @agent.event
            @dataclass(frozen=True)
            class Added:
                text: str

            @agent.event
            @dataclass(frozen=True)
            class Requested:
                text: str

            @agent.event
            @dataclass(frozen=True)
            class Greeted:
                text: str

            @agent.reduce(Added)
            def on_added(state, event):
                return replace(state, messages=state.messages + (event.text,))

            @agent.reduce(Greeted)
            def on_greeted(state, event):
                return replace(state, greeting=event.text)

            @agent.react(Added)
            def request_greeting(state, event):
                return Requested(text=event.text)

            @agent.effect(Requested)
            async def greet(effect, context):
                return Greeted(text=f"{context.text}, {effect.text}")

            agent.terminal(Greeted, status="completed")
            return agent, Greeting("Hello")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "resume.db"
            stream_id = run_stream_id("resume-smoke", "run-1")

            # Generation 1: append the first event and drive only the
            # reaction, producing the effect request -- the effect itself
            # is deliberately never run by this generation.
            agent1, resource1 = build()
            runtime1 = agent1.build_runtime(context=resource1)

            async def start(runtime):
                store = await SQLiteEventStore.open(path)
                await store.append(
                    stream_id, -1, [Event("Added", {"text": "Ostap"})]
                )
                reactions = DurableDispatcher(
                    agent=runtime.agent, store=store, subscription_name="reactions"
                )
                for _ in range(10):
                    if not await reactions.run_once():
                        break

            run(start(runtime1))
            del agent1, runtime1, resource1

            # Generation 2: nothing above survives in memory -- a fresh
            # Agent, a fresh resource instance, a reopened store.
            agent2, resource2 = build()
            runtime2 = agent2.build_runtime(context=resource2)

            async def finish():
                store = await SQLiteEventStore.open(path)
                reactions = DurableDispatcher(
                    agent=runtime2.agent, store=store, subscription_name="reactions"
                )
                effects = DurableEffectDispatcher(
                    agent=runtime2.agent,
                    store=store,
                    effects=runtime2.effects,
                    context=runtime2.context,
                    subscription_name="effects",
                )
                for _ in range(10):
                    if not await reactions.run_once():
                        break
                for _ in range(10):
                    if not await effects.run_once():
                        break
                return await store.load(stream_id)

            history = run(finish())

        event_types = [envelope.event.event_type for envelope in history]
        self.assertEqual(event_types, ["Added", "Requested", "Greeted"])
        self.assertEqual(history[-1].event.data["text"], "Hello, Ostap")
        self.assertTrue(
            runtime2.agent.is_terminal(
                history, through_version=history[-1].stream_version
            )
        )
        state = runtime2.agent.rebuild(history)
        self.assertEqual(state.messages, ("Ostap",))
        self.assertEqual(state.greeting, "Hello, Ostap")


if __name__ == "__main__":
    unittest.main()
