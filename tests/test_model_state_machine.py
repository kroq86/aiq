"""Differential property-based testing: a pure `ReferenceModel`
(`Δ(s,H,e) = (R(s,e), H|e, F(R(s,e),e))`, no store/dispatcher at all) driven
in lockstep with the real `AgentDefinition`/`DurableDispatcher`/
`DurableEffectDispatcher` core (deliberately no FastAPI/SSE -- that surface
is already covered by tests/test_e2e_scenarios.py and would only slow down
the state machine here).

Scope is intentionally narrow (per the reviewed design): six operations --
create_run, submit_command, run_reaction_once, run_effect_once,
restart_runtime, switch_definition_version -- and six invariants checked
after every step:

    state_runtime            == state_reference
    history                  append-only, sequential stream_version
    terminal_count(run)      <= 1
    causation_position(cause)  < position(effect)
    checkpoint(v1)            independent of checkpoint(v2)
    DefinitionMismatch(r1) does not propagate out of run_once()

`reduce`/`react` are the SAME plain functions used both to build the real
`AgentDefinition` and to drive the reference model -- this is deliberate:
the point is not to re-verify that shared logic against itself, it is to
verify the *infrastructure* around it (store ordering, rebuild/
rebuild_through, checkpoint bookkeeping, terminal cutoff, definition
mismatch skip) by running two independently-implemented interpreters of
the same rules and diffing them after every randomly generated step.
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from dataclasses import dataclass, replace
from functools import partial

# Expected, informational noise from the definition-mismatch skip path
# (see runtime.py's DurableDispatcher/DurableEffectDispatcher.run_once) --
# this state machine deliberately triggers it constantly by interleaving
# two versions, so it would otherwise drown out real test output.
logging.getLogger("agentlog.runtime").setLevel(logging.ERROR)

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from agentlog import (
    AgentDefinition,
    DefinitionMismatchError,
    DurableDispatcher,
    DurableEffectDispatcher,
    EffectContext,
    EffectRegistry,
    Event,
    InMemoryEventStore,
    TerminalEventConflictError,
    agent_owns_stream,
    effect_request,
)
from agentlog.runtime import _commit_outputs_with_retry, _normalize_effect_outputs


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    failure_reason: str | None = None


TERMINAL_STATUS = {"RunCompleted": "completed", "RunFailed": "failed"}
TERMINAL_TYPES = frozenset(TERMINAL_STATUS)

VERSIONS = ("v1", "v2")


def reduce(state: ChatState, event: Event) -> ChatState:
    if event.event_type == "UserMessageAdded":
        return replace(state, messages=state.messages + (str(event.data["text"]),))
    if event.event_type == "AnswerProduced":
        return replace(state, answer=str(event.data["text"]))
    if event.event_type == "RunFailed":
        return replace(state, failure_reason=str(event.data["reason"]))
    return state


def react(state: ChatState, event: Event) -> tuple[Event, ...]:
    causation_id = str(event.event_id)
    if event.event_type == "UserMessageAdded":
        return (
            effect_request(
                "ModelCallRequested",
                {"text": event.data["text"]},
                {"causation_id": causation_id},
            ),
        )
    if event.event_type == "ModelCallSucceeded":
        return (
            Event(
                "AnswerProduced",
                {"text": event.data["text"]},
                {"causation_id": causation_id},
            ),
            Event("RunCompleted", {}, {"causation_id": causation_id}),
        )
    if event.event_type == "ModelCallFailed":
        return (
            Event(
                "RunFailed",
                {"reason": str(event.data["reason"])},
                {"causation_id": causation_id},
            ),
        )
    if event.event_type == "TriggerTerminalConflict":
        # Deliberately buggy definition: a single reaction producing two
        # terminal event types in one batch. Used only by
        # produce_two_terminal_outputs to prove TerminalEventConflictError
        # actually propagates -- this is a real definition bug, unlike
        # DefinitionMismatchError, and must not be silently absorbed.
        return (
            Event("RunCompleted", {}, {"causation_id": causation_id}),
            Event("RunFailed", {"reason": "conflict"}, {"causation_id": causation_id}),
        )
    return ()


async def effect_handler(event: Event, state: ChatState, context: EffectContext) -> tuple[Event, ...]:
    should_fail = context.require("should_fail_flag")
    if should_fail[0]:
        return (Event("ModelCallFailed", {"reason": "boom"}),)
    return (Event("ModelCallSucceeded", {"text": f"echo: {event.data['text']}"}),)


@dataclass(frozen=True)
class ReferenceRun:
    state: ChatState = ChatState()
    history: tuple[Event, ...] = ()
    status: str = "active"


def reference_apply(reference_run: ReferenceRun, event: Event) -> ReferenceRun:
    if reference_run.status != "active":
        raise AssertionError(
            "terminal run is absorbing -- the real system fed the "
            "reference model an event past its terminal event"
        )
    new_state = reduce(reference_run.state, event)
    outputs = react(new_state, event)
    terminal_outputs = [output for output in outputs if output.event_type in TERMINAL_TYPES]
    if len(terminal_outputs) > 1:
        raise AssertionError("terminal conflict in reference model outputs")
    status = (
        TERMINAL_STATUS[event.event_type]
        if event.event_type in TERMINAL_TYPES
        else reference_run.status
    )
    return ReferenceRun(
        state=new_state, history=reference_run.history + (event,), status=status
    )


class AgentlogModelMachine(RuleBasedStateMachine):
    runs: Bundle = Bundle("runs")

    def __init__(self) -> None:
        super().__init__()
        self.store = InMemoryEventStore()
        self.agents: dict[str, AgentDefinition] = {}
        self.reaction_dispatchers: dict[str, DurableDispatcher] = {}
        self.effect_dispatchers: dict[str, DurableEffectDispatcher] = {}
        self.should_fail_flags: dict[str, list[bool]] = {}
        self.reference: dict[str, ReferenceRun] = {}
        self.run_version: dict[str, str] = {}
        self.pending_effect_output: dict[str, tuple] = {}
        self.poisoned_versions: set[str] = set()
        self._counter = 0

    # -- fixture wiring --------------------------------------------------

    def _ensure_version(self, version: str) -> None:
        if version in self.agents:
            return
        agent: AgentDefinition[ChatState] = AgentDefinition(
            "assistant",
            initial_state=ChatState,
            terminal_event_types=TERMINAL_TYPES,
            terminal_status_by_event_type=TERMINAL_STATUS,
            definition_version=version,
        )

        @agent.reducer
        def _reduce(state: ChatState, event: Event) -> ChatState:
            return reduce(state, event)

        for event_type in (
            "UserMessageAdded",
            "ModelCallSucceeded",
            "ModelCallFailed",
            "TriggerTerminalConflict",
        ):
            agent.react(event_type)(lambda event, state: react(state, event))

        self.agents[version] = agent

        if version in self.poisoned_versions:
            # A real TerminalEventConflictError was produced under this
            # version earlier: the offending event is still permanently
            # sitting in the store, un-checkpointed. A restart does not
            # fix that -- the *same* conflict would be encountered again
            # immediately -- so dispatchers for this version are
            # deliberately never (re)created. Only a real fix to the
            # definition or manual store surgery could recover it; this
            # harness models "give up on it", matching how an actual
            # operator would have to intervene rather than just restart.
            return

        flag = [False]
        self.should_fail_flags[version] = flag

        effects: EffectRegistry[ChatState] = EffectRegistry()

        @effects.effect("ModelCallRequested")
        async def _effect(event: Event, state: ChatState, context: EffectContext):
            return await effect_handler(event, state, context)

        owns_stream = partial(agent_owns_stream, "assistant")

        self.reaction_dispatchers[version] = DurableDispatcher(
            agent=agent,
            store=self.store,
            subscription_name=f"assistant:{version}:reactions",
            owns_stream=owns_stream,
        )
        self.effect_dispatchers[version] = DurableEffectDispatcher(
            agent=agent,
            store=self.store,
            effects=effects,
            context=EffectContext({"should_fail_flag": flag}),
            subscription_name=f"assistant:{version}:effects",
            owns_stream=owns_stream,
        )

    def _sync_reference(self, stream_id: str) -> None:
        history = run(self.store.load(stream_id))
        reference_run = self.reference[stream_id]
        for envelope in history[len(reference_run.history):]:
            reference_run = reference_apply(reference_run, envelope.event)
        self.reference[stream_id] = reference_run

        agent = self.agents[self.run_version[stream_id]]
        real_state = agent.rebuild(history)
        assert real_state == reference_run.state, (
            stream_id,
            real_state,
            reference_run.state,
        )

    def _checkpoint_snapshot(self) -> dict[str, tuple[int, int]]:
        return {
            version: (
                run(self.store.load_checkpoint(f"assistant:{version}:reactions")),
                run(self.store.load_checkpoint(f"assistant:{version}:effects")),
            )
            for version in self.reaction_dispatchers
        }

    # -- rules -------------------------------------------------------------

    @rule(target=runs, version=st.sampled_from(VERSIONS))
    def create_run(self, version: str) -> str:
        self._ensure_version(version)
        self._counter += 1
        stream_id = f"assistant:run-{self._counter}"
        run(
            self.store.append(
                stream_id,
                -1,
                [
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": version},
                    )
                ],
            )
        )
        self.reference[stream_id] = ReferenceRun()
        self.run_version[stream_id] = version
        self._sync_reference(stream_id)
        return stream_id

    @rule(
        stream_id=runs,
        text=st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Nd")),
            min_size=1,
            max_size=6,
        ),
    )
    def submit_command(self, stream_id: str, text: str) -> None:
        if stream_id not in self.reference or self.reference[stream_id].status != "active":
            return
        history = run(self.store.load(stream_id))
        run(
            self.store.append(
                stream_id,
                history[-1].stream_version,
                [Event("UserMessageAdded", {"text": text})],
            )
        )
        self._sync_reference(stream_id)

    @rule(version=st.sampled_from(VERSIONS))
    def run_reaction_once(self, version: str) -> None:
        if version not in self.reaction_dispatchers:
            return
        before = self._checkpoint_snapshot()
        try:
            run(self.reaction_dispatchers[version].run_once())
        except DefinitionMismatchError:
            raise AssertionError(
                "DefinitionMismatchError must be handled/skipped internally, "
                "never propagate"
            ) from None
        # TerminalEventConflictError is deliberately NOT caught here: it
        # means the *current* definition produced a genuinely contradictory
        # batch and must propagate to fail the worker -- see
        # produce_two_terminal_outputs for the rule that deliberately
        # triggers and expects exactly this.
        after = self._checkpoint_snapshot()
        for other_version, checkpoints in before.items():
            if other_version == version:
                continue
            assert after[other_version] == checkpoints, (
                "a different version's checkpoint moved",
                other_version,
                checkpoints,
                after[other_version],
            )
        for stream_id in list(self.reference):
            self._sync_reference(stream_id)

    @rule(version=st.sampled_from(VERSIONS))
    def run_effect_once(self, version: str) -> None:
        if version not in self.effect_dispatchers:
            return
        before = self._checkpoint_snapshot()
        try:
            run(self.effect_dispatchers[version].run_once())
        except DefinitionMismatchError:
            raise AssertionError(
                "DefinitionMismatchError must be handled/skipped internally, "
                "never propagate"
            ) from None
        # TerminalEventConflictError is deliberately NOT caught here -- see
        # the comment in run_reaction_once.
        after = self._checkpoint_snapshot()
        for other_version, checkpoints in before.items():
            if other_version == version:
                continue
            assert after[other_version] == checkpoints, (
                "a different version's checkpoint moved",
                other_version,
                checkpoints,
                after[other_version],
            )
        for stream_id in list(self.reference):
            self._sync_reference(stream_id)

    @rule(stream_id=runs)
    def compute_pending_effect_result(self, stream_id: str) -> None:
        """Models 'the effect handler already ran and computed a result,
        but has not committed yet' as explicit machine state -- the real
        dispatcher does this synchronously inside one run_once() call, but
        the race this is built to explore is exactly the gap between
        computing and committing, so it needs to be a step Hypothesis can
        interleave with others (like force_terminal_event) rather than an
        atomic black box."""
        if stream_id not in self.reference or stream_id in self.pending_effect_output:
            return
        version = self.run_version[stream_id]
        if version not in self.should_fail_flags:
            return
        history = run(self.store.load(stream_id))
        if not history or history[-1].event.event_type != "ModelCallRequested":
            return
        consumed = history[-1]
        should_fail = self.should_fail_flags[version][0]
        if should_fail:
            outputs = (Event("ModelCallFailed", {"reason": "boom"}),)
        else:
            outputs = (
                Event(
                    "ModelCallSucceeded",
                    {"text": f"echo: {consumed.event.data['text']}"},
                ),
            )
        self.pending_effect_output[stream_id] = (consumed, outputs)

    @rule(stream_id=runs)
    def force_terminal_event(self, stream_id: str) -> None:
        """Models an independent mechanism concluding the run through a
        path this fixture doesn't otherwise generate (e.g. a concurrent
        writer) -- needed to construct 'the run became terminal while an
        effect result was already computed and pending', not reachable
        through submit_command alone since that already refuses to act on
        a terminal run, same as the real command endpoint's 409."""
        if stream_id not in self.reference or self.reference[stream_id].status != "active":
            return
        history = run(self.store.load(stream_id))
        causation_id = str(history[-1].event.event_id)
        run(
            self.store.append(
                stream_id,
                history[-1].stream_version,
                [Event("RunFailed", {"reason": "forced"}, {"causation_id": causation_id})],
            )
        )
        self._sync_reference(stream_id)

    @rule(stream_id=runs)
    def commit_pending_effect_result(self, stream_id: str) -> None:
        """Delivers a result computed by compute_pending_effect_result,
        exercising _commit_outputs_with_retry directly against whatever
        the stream's *current* state actually is now -- which may have
        become terminal in the meantime via force_terminal_event. Terminal
        must be absorbing: H_{t+k} = H_t for any k, even for outputs that
        were legitimately computed before the run became terminal."""
        if stream_id not in self.pending_effect_output:
            return
        consumed, outputs = self.pending_effect_output.pop(stream_id)
        version = self.run_version[stream_id]

        history_before = run(self.store.load(stream_id))
        terminal_before = any(
            envelope.event.event_type in TERMINAL_TYPES for envelope in history_before
        )

        run(
            _commit_outputs_with_retry(
                store=self.store,
                subscription_name=f"assistant:{version}:effects",
                stream_id=stream_id,
                consumed=consumed,
                outputs=_normalize_effect_outputs(
                    consumed.event,
                    outputs,
                    operation_id=str(consumed.event.event_id),
                ),
                agent=self.agents[version],
            )
        )

        history_after = run(self.store.load(stream_id))
        if terminal_before:
            assert len(history_after) == len(history_before), (
                "a pending effect result was appended after the run was "
                "already terminal",
                stream_id,
            )

        if stream_id in self.reference:
            self._sync_reference(stream_id)

    @rule(stream_id=runs)
    def produce_two_terminal_outputs(self, stream_id: str) -> None:
        """Deliberately buggy definition path: TriggerTerminalConflict's
        reaction returns two terminal event types in one batch.
        TerminalEventConflictError must propagate out of run_once() (unlike
        DefinitionMismatchError) and the conflicting batch must not commit
        any part of itself -- terminalCount(H) stays 0, not 1 or 2."""
        if stream_id not in self.reference or self.reference[stream_id].status != "active":
            return
        version = self.run_version[stream_id]
        if version not in self.reaction_dispatchers:
            return
        # Drain whatever this version's reaction dispatcher already had
        # pending *first* -- otherwise the single run_once() call below
        # could process an earlier, unrelated pending event instead of the
        # TriggerTerminalConflict this rule is about to append, since the
        # checkpoint is shared across every stream of this version.
        for _ in range(50):
            if not run(self.reaction_dispatchers[version].run_once()):
                break
        for other_stream_id in list(self.reference):
            self._sync_reference(other_stream_id)
        if stream_id not in self.reference or self.reference[stream_id].status != "active":
            # Draining may have completed/terminated this exact run
            # legitimately (e.g. its own effect result was already
            # pending) -- nothing left to trigger a conflict on.
            return

        history = run(self.store.load(stream_id))
        run(
            self.store.append(
                stream_id,
                history[-1].stream_version,
                [Event("TriggerTerminalConflict", {})],
            )
        )

        raised = False
        try:
            run(self.reaction_dispatchers[version].run_once())
        except TerminalEventConflictError:
            raised = True
        assert raised, (
            "a real terminal conflict must propagate as "
            "TerminalEventConflictError, not be silently absorbed"
        )

        final_history = run(self.store.load(stream_id))
        terminal_count = sum(
            1 for envelope in final_history if envelope.event.event_type in TERMINAL_TYPES
        )
        assert terminal_count == 0, (
            "a conflicting batch must not commit any part of itself",
            terminal_count,
        )

        # The checkpoint never advanced past the conflicting event (by
        # design -- there is no safe way to skip it, unlike
        # DefinitionMismatchError), so *every* other stream sharing this
        # version's "reactions" subscription is now permanently stuck too
        # -- exactly mirroring the real fastapi.py worker: one dispatcher
        # raising takes the whole worker unhealthy, not just one run. This
        # also survives restart_runtime: the offending event is still
        # sitting un-checkpointed in the store, so a fresh dispatcher would
        # hit the exact same conflict again immediately -- poisoned_versions
        # makes _ensure_version refuse to (re)create dispatchers for this
        # version at all, matching "needs a real fix or manual operator
        # intervention, not just a restart".
        self.poisoned_versions.add(version)
        del self.reaction_dispatchers[version]
        del self.effect_dispatchers[version]
        del self.reference[stream_id]
        del self.run_version[stream_id]
        self.pending_effect_output.pop(stream_id, None)

    @rule(version=st.sampled_from(VERSIONS))
    def toggle_effect_failure(self, version: str) -> None:
        if version not in self.should_fail_flags:
            return
        flag = self.should_fail_flags[version]
        flag[0] = not flag[0]

    @rule(version=st.sampled_from(VERSIONS))
    def switch_definition_version(self, version: str) -> None:
        """Simulates a deploy activating a version's runtime before any
        run of that version necessarily exists yet."""
        self._ensure_version(version)

    @rule()
    def restart_runtime(self) -> None:
        """Simulates a process restart: every Agent/dispatcher object for
        every known version is thrown away and rebuilt from scratch --
        only self.store (persisted history + checkpoints) survives."""
        self.agents.clear()
        self.reaction_dispatchers.clear()
        self.effect_dispatchers.clear()
        self.should_fail_flags.clear()
        for version in set(self.run_version.values()):
            self._ensure_version(version)

    # -- invariants ----------------------------------------------------

    @invariant()
    def store_is_append_only_with_sequential_versions(self) -> None:
        for stream_id in self.reference:
            history = run(self.store.load(stream_id))
            versions = [envelope.stream_version for envelope in history]
            assert versions == list(range(len(history)))

    @invariant()
    def at_most_one_terminal_event_per_run(self) -> None:
        for stream_id in self.reference:
            history = run(self.store.load(stream_id))
            terminal_count = sum(
                1 for envelope in history if envelope.event.event_type in TERMINAL_TYPES
            )
            assert terminal_count <= 1

    @invariant()
    def causation_points_strictly_backward(self) -> None:
        for stream_id in self.reference:
            history = run(self.store.load(stream_id))
            positions = {
                str(envelope.event.event_id): index
                for index, envelope in enumerate(history)
            }
            for index, envelope in enumerate(history):
                causation_id = envelope.event.metadata.get("causation_id")
                if envelope.event.event_type in ("RunCreated", "UserMessageAdded"):
                    # RunCreated is the system root; UserMessageAdded is
                    # command-produced -- matches agentlog.framework's own
                    # convention of causation_source=None for command
                    # output (Agent.handle_command). Everything downstream
                    # of those two roots is caused by something.
                    continue
                assert causation_id is not None, (
                    "non-root event missing causation_id",
                    stream_id,
                    envelope.event.event_type,
                )
                assert causation_id in positions
                assert positions[causation_id] < index


TestAgentlogModel = AgentlogModelMachine.TestCase
TestAgentlogModel.settings = settings(
    max_examples=100,
    stateful_step_count=60,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


if __name__ == "__main__":
    unittest.main()
