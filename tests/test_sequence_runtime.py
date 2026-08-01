from __future__ import annotations

import asyncio
import hashlib
import unittest
from dataclasses import dataclass

from agentlog import (
    Agent,
    ArtifactRef,
    ChildTerminalOutcome,
    DurableDispatcher,
    DurableEffectDispatcher,
    Event,
    InMemoryEventStore,
    Sequence,
    SequenceChild,
    SequenceDefinition,
    run_stream_id,
)


def run(awaitable):
    return asyncio.run(awaitable)


@dataclass(frozen=True)
class Start:
    input_ref: dict | None = None


class ChildRuntime:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.starts = []
        self.waits = []
        self.run_ids = set()

    async def ensure_started(self, child, *, child_run_id, operation_id, input_ref):
        self.starts.append((child, child_run_id, operation_id, input_ref))
        self.run_ids.add(child_run_id)
        return child_run_id

    async def wait_terminal(self, child, *, child_run_id, operation_id):
        self.waits.append((child, child_run_id, operation_id))
        outcome = self.outcomes.pop(0)
        return ChildTerminalOutcome(
            child_run_id,
            outcome.status,
            outcome.output_ref,
            outcome.failure,
        )


def output(name: str, version: int) -> ArtifactRef:
    digest = hashlib.sha256(f"{name}-{version}".encode()).hexdigest()
    return ArtifactRef(name, str(version), "application/json", f"sha256:{digest}", 2)


def define(runtime, *, children=None):
    child_definitions = children or (
        SequenceChild("first", "1"),
        SequenceChild("second", "1"),
    )
    definition = SequenceDefinition(
        "pipeline",
        "1",
        child_definitions,
    )
    policy = Sequence(definition=definition, start_on=Start, child_runtime="children")
    agent = Agent(name="pipeline", version="1", initial_state=policy.initial_state)
    agent.event(Start)
    policy.install(agent)

    @agent.command("start")
    def start(payload):
        return Start(payload.get("input_ref"))

    return agent, agent.build_runtime(context={"children": runtime})


class SequenceRuntimeTests(unittest.TestCase):
    def drive(self, store, runtime, *, rounds=30):
        reactions = DurableDispatcher(
            agent=runtime.agent,
            store=store,
            subscription_name="pipeline:1:reactions",
        )
        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="pipeline:1:effects",
        )
        for _ in range(rounds):
            progressed = run(reactions.run_once())
            progressed = run(effects.run_once()) or progressed
            if not progressed:
                break

    def start_run(self, store, agent, run_id):
        stream = run_stream_id("pipeline", run_id)
        run(
            store.append(
                stream,
                -1,
                (
                    Event("RunCreated", {"agent": "pipeline", "definition_version": "1"}),
                    agent.handle_command("start", {})[0],
                ),
            )
        )
        return stream

    def test_linear_children_use_exact_output_ref_and_complete_parent(self):
        first, second = output("first-output", 1), output("second-output", 4)
        children = ChildRuntime(
            (
                ChildTerminalOutcome("placeholder", "completed", first),
                ChildTerminalOutcome("placeholder", "completed", second),
            )
        )
        # The fake substitutes the committed identity, like a separately persisted child run.
        original_wait = children.wait_terminal

        async def wait(child, *, child_run_id, operation_id):
            outcome = await original_wait(
                child, child_run_id=child_run_id, operation_id=operation_id
            )
            return ChildTerminalOutcome(child_run_id, outcome.status, outcome.output_ref)

        children.wait_terminal = wait
        store = InMemoryEventStore()
        agent, runtime = define(children)
        stream = self.start_run(store, agent, "happy")
        self.drive(store, runtime)

        history = run(store.load(stream))
        self.assertEqual(history[-1].event.event_type, "SequenceCompleted")
        self.assertEqual([start[3] for start in children.starts], [None, first])
        self.assertEqual(len({start[1] for start in children.starts}), 2)

    def test_child_failure_is_fail_fast(self):
        children = ChildRuntime((ChildTerminalOutcome("placeholder", "failed", failure="boom"),))

        async def fail(child, *, child_run_id, operation_id):
            del child, operation_id
            return ChildTerminalOutcome(child_run_id, "failed", failure="boom")

        children.wait_terminal = fail
        store = InMemoryEventStore()
        agent, runtime = define(children)
        stream = self.start_run(store, agent, "failed")
        self.drive(store, runtime)
        types = [item.event.event_type for item in run(store.load(stream))]
        self.assertEqual(types[-1], "SequenceFailed")
        self.assertEqual(types.count("ChildStartRequested"), 1)

    def test_fresh_runtime_reuses_committed_child_start_identity(self):
        ref = output("out", 1)
        children = ChildRuntime(())

        async def completed(child, *, child_run_id, operation_id):
            del child, operation_id
            return ChildTerminalOutcome(child_run_id, "completed", ref)

        children.wait_terminal = completed
        store = InMemoryEventStore()
        agent, runtime = define(children)
        stream = self.start_run(store, agent, "restart")
        reactions = DurableDispatcher(
            agent=runtime.agent,
            store=store,
            subscription_name="pipeline:1:reactions",
        )
        for _ in range(5):
            run(reactions.run_once())
            history = run(store.load(stream))
            if any(e.event.event_type == "ChildStartRequested" for e in history):
                break
        requested = next(e.event for e in history if e.event.event_type == "ChildStartRequested")

        fresh_agent, fresh_runtime = define(children)
        del fresh_agent
        self.drive(store, fresh_runtime)
        first_start = children.starts[0]
        self.assertEqual(first_start[1], str(requested.metadata["operation_id"]))
        self.assertEqual(first_start[1], first_start[2])

    def test_duplicate_parent_start_does_not_allocate_another_child(self):
        ref = output("out", 1)
        children = ChildRuntime(())

        async def completed(child, *, child_run_id, operation_id):
            del child, operation_id
            return ChildTerminalOutcome(child_run_id, "completed", ref)

        children.wait_terminal = completed
        store = InMemoryEventStore()
        agent, runtime = define(children, children=(SequenceChild("only", "1"),))
        stream = run_stream_id("pipeline", "duplicate-start")
        start = agent.handle_command("start", {})[0]
        run(
            store.append(
                stream,
                -1,
                (
                    Event("RunCreated", {"agent": "pipeline", "definition_version": "1"}),
                    start,
                    agent.handle_command("start", {})[0],
                ),
            )
        )
        # Rebuild the policy/runtime after both input facts are durable. The
        # arbitration must come from history, not retained in-memory state.
        fresh_agent, fresh_runtime = define(
            children, children=(SequenceChild("only", "1"),)
        )
        del fresh_agent, runtime
        self.drive(store, fresh_runtime)
        types = [item.event.event_type for item in run(store.load(stream))]
        # Both accepted input facts remain observable; only the first may
        # produce child orchestration.
        self.assertEqual(types.count("SequenceStarted"), 2)
        self.assertEqual(types.count("ChildStartRequested"), 1)
        self.assertEqual(types.count("ChildStarted"), 1)
        self.assertEqual(len(children.starts), 1)
        self.assertEqual(children.starts[0][0].agent_name, "only")


if __name__ == "__main__":
    unittest.main()
