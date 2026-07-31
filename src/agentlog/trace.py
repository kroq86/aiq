from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .core import EventEnvelope, EventStore, JsonValue
from .runtime import AgentDefinition
from .streams import run_stream_id


class RunNotFoundError(Exception):
    """No event stream exists for the requested agent_name/run_id pair.

    Deliberately also raised for an unregistered agent_name and for a run_id
    that belongs to a different agent: the trace API must not let a caller
    distinguish "wrong agent" from "no such run" for a stream it cannot read.
    """


def _plain_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One stored event, flattened to plain data for an external consumer."""

    event_id: str
    event_type: str
    stream_id: str
    stream_version: int
    global_position: int
    correlation_id: str | None
    causation_id: str | None
    operation_id: str | None
    data: JsonValue
    metadata: JsonValue
    created_at: str


@dataclass(frozen=True, slots=True)
class CausalEdge:
    """A causation_id on `effect_event_id` that resolved to `cause_event_id`
    within this same trace."""

    cause_event_id: str
    effect_event_id: str


@dataclass(frozen=True, slots=True)
class DanglingCausation:
    """An event that names a causation_id which does not resolve to any
    event in this trace. Kept distinct from `roots`: this event *claims* a
    cause, it just can't be resolved here -- unlike a root, which claims
    none."""

    event_id: str
    missing_causation_id: str


_DEFAULT_TERMINAL_STATUS: Mapping[str, str] = {
    "RunCompleted": "completed",
    "RunFailed": "failed",
    "RunCancelled": "cancelled",
}


@dataclass(frozen=True, slots=True)
class CausalTrace:
    """A disposable, recomputable projection of one run's immutable stream.

    Nothing here is persisted separately from the event log itself -- this
    is derived fresh from `EventEnvelope` history every time, never cached
    or checkpointed.
    """

    agent_name: str
    run_id: str
    events: tuple[TraceEvent, ...]
    edges: tuple[CausalEdge, ...]
    roots: tuple[str, ...]
    dangling_causation: tuple[DanglingCausation, ...]
    terminal: bool
    terminal_event_type: str | None
    latest_stream_version: int
    terminal_status_by_event_type: Mapping[str, str] = field(default_factory=dict)

    @property
    def terminal_status(self) -> str:
        if self.terminal_event_type is None:
            return "active"
        mapping = self.terminal_status_by_event_type or _DEFAULT_TERMINAL_STATUS
        return mapping.get(self.terminal_event_type, self.terminal_event_type)


def _trace_event_from_envelope(envelope: EventEnvelope) -> TraceEvent:
    metadata = envelope.event.metadata
    correlation_id = metadata.get("correlation_id")
    causation_id = metadata.get("causation_id")
    operation_id = metadata.get("operation_id")
    return TraceEvent(
        event_id=str(envelope.event.event_id),
        event_type=envelope.event.event_type,
        stream_id=envelope.stream_id,
        stream_version=envelope.stream_version,
        global_position=envelope.global_position,
        correlation_id=str(correlation_id) if correlation_id is not None else None,
        causation_id=str(causation_id) if causation_id is not None else None,
        operation_id=str(operation_id) if operation_id is not None else None,
        data=_plain_json(envelope.event.data),
        metadata=_plain_json(metadata),
        created_at=envelope.created_at.isoformat(),
    )


def build_causal_trace(
    *,
    agent_name: str,
    run_id: str,
    agent: AgentDefinition[Any],
    history: Sequence[EventEnvelope],
) -> CausalTrace:
    """Pure projection: no I/O, no dispatcher/broadcaster state, just the
    already-loaded canonical history for one stream."""
    ordered_history = tuple(
        sorted(history, key=lambda envelope: envelope.stream_version)
    )
    events = tuple(
        _trace_event_from_envelope(envelope)
        for envelope in ordered_history
    )
    known_ids = {trace_event.event_id for trace_event in events}

    edges: list[CausalEdge] = []
    roots: list[str] = []
    dangling: list[DanglingCausation] = []
    for trace_event in events:
        if trace_event.causation_id is None:
            roots.append(trace_event.event_id)
        elif trace_event.causation_id in known_ids:
            edges.append(
                CausalEdge(
                    cause_event_id=trace_event.causation_id,
                    effect_event_id=trace_event.event_id,
                )
            )
        else:
            dangling.append(
                DanglingCausation(
                    event_id=trace_event.event_id,
                    missing_causation_id=trace_event.causation_id,
                )
            )

    terminal_event_type = next(
        (
            trace_event.event_type
            for trace_event in events
            if trace_event.event_type in agent.terminal_event_types
        ),
        None,
    )

    return CausalTrace(
        agent_name=agent_name,
        run_id=run_id,
        events=events,
        edges=tuple(edges),
        roots=tuple(roots),
        dangling_causation=tuple(dangling),
        terminal=terminal_event_type is not None,
        terminal_event_type=terminal_event_type,
        latest_stream_version=(
            ordered_history[-1].stream_version if ordered_history else -1
        ),
        terminal_status_by_event_type=agent.terminal_status_by_event_type,
    )


def trace_to_json(trace: CausalTrace) -> dict[str, Any]:
    """Plain, JSON-ready dict -- the wire shape for an HTTP trace endpoint
    or any other external consumer."""
    return {
        "schema_version": 1,
        "graph_kind": "domain-event-history",
        "agent_name": trace.agent_name,
        "run_id": trace.run_id,
        "terminal_status": trace.terminal_status,
        "latest_stream_version": trace.latest_stream_version,
        "roots": list(trace.roots),
        "nodes": [
            {
                "event_id": trace_event.event_id,
                "event_type": trace_event.event_type,
                "stream_id": trace_event.stream_id,
                "stream_version": trace_event.stream_version,
                "global_position": trace_event.global_position,
                "correlation_id": trace_event.correlation_id,
                "causation_id": trace_event.causation_id,
                "operation_id": trace_event.operation_id,
                "data": trace_event.data,
                "metadata": trace_event.metadata,
                "created_at": trace_event.created_at,
            }
            for trace_event in trace.events
        ],
        "edges": [
            {
                "source_event_id": edge.cause_event_id,
                "target_event_id": edge.effect_event_id,
                "kind": "caused",
            }
            for edge in trace.edges
        ],
        "timeline": [
            {
                "event_id": trace_event.event_id,
                "stream_version": trace_event.stream_version,
                "global_position": trace_event.global_position,
            }
            for trace_event in trace.events
        ],
        "dangling_causation": [
            {
                "event_id": item.event_id,
                "missing_causation_id": item.missing_causation_id,
            }
            for item in trace.dangling_causation
        ],
    }


class TraceService:
    """The only I/O boundary for trace export: loads one stream's canonical
    history, then hands it to the pure `build_causal_trace`. Never touches
    effects, adapters, or in-memory dispatcher/broadcaster state."""

    def __init__(
        self,
        *,
        store: EventStore,
        agents: Mapping[str, AgentDefinition[Any]],
    ) -> None:
        self._store = store
        self._agents = agents

    async def export(self, agent_name: str, run_id: str) -> CausalTrace:
        agent = self._agents.get(agent_name)
        if agent is None:
            raise RunNotFoundError(f"unknown agent: {agent_name!r}")
        history = await self._store.load(run_stream_id(agent_name, run_id))
        if not history:
            raise RunNotFoundError(
                f"no run {run_id!r} for agent {agent_name!r}"
            )
        return build_causal_trace(
            agent_name=agent_name,
            run_id=run_id,
            agent=agent,
            history=history,
        )
