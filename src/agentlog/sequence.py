"""Contracts for fail-fast durable sequential child composition."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Protocol

from .artifacts import ArtifactRef
from .framework import Agent, DefinitionError


@dataclass(frozen=True, slots=True)
class SequenceChild:
    agent_name: str
    definition_version: str

    def __post_init__(self) -> None:
        if not self.agent_name or not self.definition_version:
            raise ValueError("sequence child agent name and version must not be empty")


@dataclass(frozen=True, slots=True)
class SequenceDefinition:
    sequence_id: str
    version: str
    children: tuple[SequenceChild, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))
        if not self.sequence_id or not self.version:
            raise ValueError("sequence identity and version must not be empty")
        if not self.children:
            raise ValueError("sequence requires at least one child")


ChildStatus = Literal["pending", "active", "completed", "failed"]
ParentStatus = Literal["idle", "active", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class SequenceState:
    definition_id: str
    current_child_index: int = 0
    child_run_ids: tuple[str | None, ...] = ()
    child_statuses: tuple[ChildStatus, ...] = ()
    accepted_outputs: tuple[ArtifactRef | None, ...] = ()
    parent_status: ParentStatus = "idle"
    pending_child_start_operation: str | None = None
    start_events_seen: int = 0


@dataclass(frozen=True, slots=True)
class ChildTerminalOutcome:
    child_run_id: str
    status: Literal["completed", "failed"]
    output_ref: ArtifactRef | None = None
    failure: str | None = None

    def __post_init__(self) -> None:
        if not self.child_run_id:
            raise ValueError("child outcome requires child_run_id")
        if self.status == "completed" and self.failure is not None:
            raise ValueError("completed child outcome cannot contain failure")
        if self.status == "failed" and not self.failure:
            raise ValueError("failed child outcome requires failure")


class SequenceChildRuntime(Protocol):
    async def ensure_started(
        self,
        child: SequenceChild,
        *,
        child_run_id: str,
        operation_id: str,
        input_ref: ArtifactRef | None,
    ) -> str: ...

    async def wait_terminal(
        self,
        child: SequenceChild,
        *,
        child_run_id: str,
        operation_id: str,
    ) -> ChildTerminalOutcome: ...


@dataclass(frozen=True, slots=True)
class SequenceStarted:
    input_ref: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ChildStartRequested:
    index: int
    input_ref: Mapping[str, Any] | None
    operation_id: str = ""


@dataclass(frozen=True, slots=True)
class ChildStarted:
    index: int
    child_run_id: str


@dataclass(frozen=True, slots=True)
class ChildOutcomeRequested:
    index: int
    child_run_id: str
    operation_id: str = ""


@dataclass(frozen=True, slots=True)
class ChildCompleted:
    index: int
    child_run_id: str
    output_ref: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class ChildFailed:
    index: int
    child_run_id: str
    failure: str


@dataclass(frozen=True, slots=True)
class SequenceCompleted:
    output_ref: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class SequenceFailed:
    child_run_id: str
    failure: str


def _ref_data(ref: ArtifactRef | None) -> Mapping[str, Any] | None:
    if ref is None:
        return None
    return {
        "name": ref.name,
        "version": ref.version,
        "media_type": ref.media_type,
        "digest": ref.digest,
        "size": ref.size,
        "created_causation": ref.created_causation,
        "storage_reference": ref.storage_reference,
    }


def _ref(value: Mapping[str, Any] | None) -> ArtifactRef | None:
    return None if value is None else ArtifactRef(**dict(value))


class Sequence:
    """Install linear, fail-fast durable orchestration on a parent agent."""

    name = "sequence"

    def __init__(
        self,
        *,
        definition: SequenceDefinition,
        start_on: type,
        child_runtime: str,
    ) -> None:
        if not child_runtime:
            raise ValueError("child runtime resource key must not be empty")
        self.definition = definition
        self.start_on = start_on
        self.child_runtime_key = child_runtime
        self._installed = False

    def initial_state(self) -> SequenceState:
        count = len(self.definition.children)
        return SequenceState(
            definition_id=f"{self.definition.sequence_id}@{self.definition.version}",
            child_run_ids=(None,) * count,
            child_statuses=("pending",) * count,
            accepted_outputs=(None,) * count,
        )

    def install(self, agent: Agent) -> None:
        if self._installed:
            raise DefinitionError("Sequence instance is already installed")
        definition = self.definition
        resource_key = self.child_runtime_key

        def validate(resources: object) -> None:
            runtime = _resource(resources, resource_key)
            if not callable(getattr(runtime, "ensure_started", None)) or not callable(
                getattr(runtime, "wait_terminal", None)
            ):
                raise TypeError(f"resource {resource_key!r} must be SequenceChildRuntime")

        agent._claim_policy(self.name, validate_resources=validate)
        self._installed = True
        for event_type in (
            SequenceStarted,
            ChildStartRequested,
            ChildStarted,
            ChildOutcomeRequested,
            ChildCompleted,
            ChildFailed,
            SequenceCompleted,
            SequenceFailed,
        ):
            agent.event(event_type)
        agent.terminal(SequenceCompleted, status="completed")
        agent.terminal(SequenceFailed, status="failed")

        @agent.react(self.start_on)
        def begin(state, event):
            if state.parent_status != "idle":
                return None
            return SequenceStarted(getattr(event, "input_ref", None))

        @agent.reduce(SequenceStarted)
        def reduce_started(state: SequenceState, event: SequenceStarted):
            del event
            return replace(
                state,
                parent_status="active",
                start_events_seen=state.start_events_seen + 1,
            )

        @agent.react(SequenceStarted)
        def request_first(state: SequenceState, event: SequenceStarted):
            if state.start_events_seen != 1:
                return None
            return ChildStartRequested(0, event.input_ref)

        @agent.reduce(ChildStartRequested)
        def reduce_start_request(state: SequenceState, event: ChildStartRequested):
            return replace(state, pending_child_start_operation=event.operation_id)

        @agent.effect(ChildStartRequested)
        async def start_child(event: ChildStartRequested, resources):
            runtime = _resource(resources, resource_key)
            child_run_id = event.operation_id
            actual = await runtime.ensure_started(
                definition.children[event.index],
                child_run_id=child_run_id,
                operation_id=event.operation_id,
                input_ref=_ref(event.input_ref),
            )
            if actual != child_run_id:
                return SequenceFailed(actual, "child runtime changed committed run identity")
            return ChildStarted(event.index, child_run_id)

        @agent.reduce(ChildStarted)
        def reduce_child_started(state: SequenceState, event: ChildStarted):
            if event.index != state.current_child_index:
                return state
            ids = _replace_at(state.child_run_ids, event.index, event.child_run_id)
            statuses = _replace_at(state.child_statuses, event.index, "active")
            return replace(
                state,
                child_run_ids=ids,
                child_statuses=statuses,
                pending_child_start_operation=None,
            )

        @agent.react(ChildStarted)
        def request_outcome(state: SequenceState, event: ChildStarted):
            if event.index != state.current_child_index:
                return None
            return ChildOutcomeRequested(event.index, event.child_run_id)

        @agent.effect(ChildOutcomeRequested)
        async def await_outcome(event: ChildOutcomeRequested, resources):
            runtime = _resource(resources, resource_key)
            outcome = await runtime.wait_terminal(
                definition.children[event.index],
                child_run_id=event.child_run_id,
                operation_id=event.operation_id,
            )
            if outcome.child_run_id != event.child_run_id:
                return ChildFailed(event.index, event.child_run_id, "outcome child run mismatch")
            if outcome.status == "failed":
                return ChildFailed(event.index, event.child_run_id, outcome.failure or "failed")
            return ChildCompleted(event.index, event.child_run_id, _ref_data(outcome.output_ref))

        @agent.reduce(ChildCompleted)
        def reduce_completed(state: SequenceState, event: ChildCompleted):
            if not _is_current(state, event.index, event.child_run_id):
                return state
            statuses = _replace_at(state.child_statuses, event.index, "completed")
            outputs = _replace_at(state.accepted_outputs, event.index, _ref(event.output_ref))
            next_index = min(event.index + 1, len(definition.children) - 1)
            return replace(
                state,
                current_child_index=next_index,
                child_statuses=statuses,
                accepted_outputs=outputs,
            )

        @agent.react(ChildCompleted)
        def advance(state: SequenceState, event: ChildCompleted):
            if state.child_statuses[event.index] != "completed":
                return None
            if event.index == len(definition.children) - 1:
                return SequenceCompleted(event.output_ref)
            if (
                state.current_child_index != event.index + 1
                or state.child_statuses[event.index + 1] != "pending"
            ):
                return None
            return ChildStartRequested(event.index + 1, event.output_ref)

        @agent.reduce(ChildFailed)
        def reduce_failed(state: SequenceState, event: ChildFailed):
            if not _is_current(state, event.index, event.child_run_id):
                return state
            return replace(
                state,
                child_statuses=_replace_at(state.child_statuses, event.index, "failed"),
                parent_status="failed",
            )

        @agent.react(ChildFailed)
        def fail_parent(state: SequenceState, event: ChildFailed):
            if state.parent_status != "failed":
                return None
            return SequenceFailed(event.child_run_id, event.failure)

        @agent.reduce(SequenceCompleted)
        def reduce_sequence_completed(state: SequenceState, event: SequenceCompleted):
            del event
            return replace(state, parent_status="completed")


def _replace_at(values: tuple[Any, ...], index: int, value: Any) -> tuple[Any, ...]:
    return values[:index] + (value,) + values[index + 1 :]


def _is_current(state: SequenceState, index: int, child_run_id: str) -> bool:
    return (
        index == state.current_child_index
        and state.child_run_ids[index] == child_run_id
        and state.child_statuses[index] == "active"
    )


def _resource(resources: object, key: str) -> Any:
    if isinstance(resources, Mapping):
        return resources[key]
    try:
        return getattr(resources, key)
    except AttributeError as error:
        raise KeyError(f"missing sequence resource: {key!r}") from error
