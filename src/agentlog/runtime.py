from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Any, Generic, TypeVar
from uuid import UUID, uuid4

from .core import Event, EventEnvelope, EventStore, JsonValue, VersionConflictError
from .streams import agent_owns_stream

logger = logging.getLogger("agentlog.runtime")

State = TypeVar("State")
Reducer = Callable[[State, Event], State]
Reaction = Callable[[Event, State], Sequence[Event]]
EffectHandler = Callable[
    [Event, State, "EffectContext"],
    Awaitable[Sequence[Event]],
]
StreamOwnership = Callable[[str], bool]


class EffectMetadataError(ValueError):
    """Canonical effect identity metadata is missing or contradictory."""


class TerminalEventConflictError(ValueError):
    """A single reaction/effect output batch produced more than one
    terminal event type. This is a definition/reducer bug, not a domain
    failure -- left uncaught deliberately so it propagates and fails the
    worker instead of committing an ambiguous terminal fact."""


class DefinitionMismatchError(ValueError):
    """A run's `RunCreated` was recorded under a different
    (agent_name, definition_version) than the definition now interpreting
    it. `Run = (Definition, History)` -- running History under a different
    Definition than it was created under is not the same run.

    Unlike `TerminalEventConflictError`, this is not evidence that the
    *current* definition is broken -- it is normal after a deploy, while an
    old-version run is still in flight. `DurableDispatcher`/
    `DurableEffectDispatcher` catch it themselves (see `run_once`) and skip
    just that one stream's pending event -- Mismatch(r1) must not imply
    Unavailable(r2) for any other run r2. It still must never be silently
    *interpreted*: `assert_definition_matches` always raises here, and
    `fastapi.py` still turns it into a `409` for direct read/command
    access to the blocked run."""


def _validate_at_most_one_terminal_output(
    agent: "AgentDefinition[Any]", outputs: Sequence[Event]
) -> None:
    terminal_outputs = [
        output for output in outputs if output.event_type in agent.terminal_event_types
    ]
    if len(terminal_outputs) > 1:
        raise TerminalEventConflictError(
            "reaction/effect produced more than one terminal event in a "
            f"single batch: {[output.event_type for output in terminal_outputs]}"
        )


def effect_request(
    event_type: str,
    data: Mapping[str, JsonValue],
    metadata: Mapping[str, JsonValue] | None = None,
    *,
    event_id: UUID | None = None,
) -> Event:
    """Create an immutable external-effect request with explicit identity."""
    request_id = event_id or uuid4()
    expected_operation_id = str(request_id)
    request_metadata = dict(metadata or {})
    existing_operation_id = request_metadata.get("operation_id")
    if (
        existing_operation_id is not None
        and existing_operation_id != expected_operation_id
    ):
        raise EffectMetadataError(
            "effect request operation_id must equal its event_id"
        )
    request_metadata["operation_id"] = expected_operation_id
    return Event(
        event_type,
        data,
        request_metadata,
        event_id=request_id,
    )


def _validate_effect_request(event: Event) -> str:
    expected_operation_id = str(event.event_id)
    operation_id = event.metadata.get("operation_id")
    if operation_id is None:
        raise EffectMetadataError(
            "registered effect request must be created with effect_request()"
        )
    if operation_id != expected_operation_id:
        raise EffectMetadataError(
            "effect request operation_id must equal its event_id"
        )
    return expected_operation_id


def _normalize_effect_outputs(
    request: Event,
    outputs: Sequence[Event],
    *,
    operation_id: str,
) -> tuple[Event, ...]:
    causation_id = str(request.event_id)
    normalized: list[Event] = []
    for output in outputs:
        metadata = dict(output.metadata)
        existing_operation_id = metadata.get("operation_id")
        if (
            existing_operation_id is not None
            and existing_operation_id != operation_id
        ):
            raise EffectMetadataError(
                "effect result operation_id conflicts with its request"
            )
        existing_causation_id = metadata.get("causation_id")
        if (
            existing_causation_id is not None
            and existing_causation_id != causation_id
        ):
            raise EffectMetadataError(
                "effect result causation_id conflicts with its request"
            )
        metadata["operation_id"] = operation_id
        metadata["causation_id"] = causation_id
        normalized.append(
            Event(
                output.event_type,
                output.data,
                metadata,
                event_id=output.event_id,
            )
        )
    return tuple(normalized)


async def _advance_checkpoint_without_outputs(
    *,
    store: EventStore,
    subscription_name: str,
    checkpoint: int,
    consumed: EventEnvelope,
) -> None:
    # expected_stream_version is asserted by the store only when events is
    # non-empty (see EventStore.commit_subscription_batch) -- a
    # checkpoint-only advance can never conflict with a concurrent append
    # to consumed.stream_id, so there is nothing to read here.
    await store.commit_subscription_batch(
        subscription_name=subscription_name,
        expected_checkpoint=checkpoint,
        stream_id=consumed.stream_id,
        expected_stream_version=-1,
        events=(),
        new_checkpoint=consumed.global_position,
    )


async def _commit_outputs_with_retry(
    *,
    store: EventStore,
    subscription_name: str,
    stream_id: str,
    consumed: EventEnvelope,
    outputs: tuple[Event, ...],
    agent: "AgentDefinition[Any]",
    max_attempts: int = 3,
) -> None:
    """Commit `outputs` -- already computed exactly once by the caller; a
    reaction/effect handler must never be re-invoked here -- against the
    subscription checkpoint.

    A concurrent writer (typically a command appending the run's next
    domain event while this dispatcher is mid-flight) can make the
    stream-version snapshot this dispatcher read stale by the time it
    commits. On that race, only the atomic commit itself is retried, reusing
    the same immutable `outputs` batch (same event_ids/operation_id) --
    never recomputing it, so an effect's external call is never repeated
    just because of a concurrency conflict on the commit.

    Terminal must be absorbing even across that same race: `outputs` were
    computed against a snapshot where the run was not yet terminal, but a
    *different* concurrent writer may have terminated it since (e.g. a
    reaction on a later event beat this effect to the commit). Every
    attempt re-checks the *current* (unbounded, not just through
    `consumed`'s own position) terminal status fresh before committing --
    if the run has since become terminal, `outputs` are discarded and only
    the checkpoint advances, exactly like the empty-output path.
    """
    if not outputs:
        await _advance_checkpoint_without_outputs(
            store=store,
            subscription_name=subscription_name,
            checkpoint=await store.load_checkpoint(subscription_name),
            consumed=consumed,
        )
        return

    for attempt in range(max_attempts):
        checkpoint = await store.load_checkpoint(subscription_name)
        if checkpoint >= consumed.global_position:
            # Already handled by an earlier attempt whose commit actually
            # succeeded -- outputs must not be appended a second time.
            return
        history = await store.load(stream_id)
        if history and agent.is_terminal(
            history, through_version=history[-1].stream_version
        ):
            await _advance_checkpoint_without_outputs(
                store=store,
                subscription_name=subscription_name,
                checkpoint=checkpoint,
                consumed=consumed,
            )
            return
        current_stream_version = history[-1].stream_version if history else -1
        try:
            await store.commit_subscription_batch(
                subscription_name=subscription_name,
                expected_checkpoint=checkpoint,
                stream_id=stream_id,
                expected_stream_version=current_stream_version,
                events=outputs,
                new_checkpoint=consumed.global_position,
            )
            return
        except VersionConflictError:
            if attempt == max_attempts - 1:
                raise


@dataclass(frozen=True, slots=True)
class EffectContext:
    """Explicitly injected infrastructure adapters for effect handlers."""

    adapters: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adapters",
            MappingProxyType(dict(self.adapters)),
        )

    def require(self, name: str) -> Any:
        try:
            return self.adapters[name]
        except KeyError as error:
            raise LookupError(f"effect adapter is not configured: {name!r}") from error


class EffectRegistry(Generic[State]):
    """Runtime-owned async I/O handlers, separate from the agent definition."""

    def __init__(self) -> None:
        self._handlers: dict[str, EffectHandler[State]] = {}

    def effect(
        self,
        event_type: str,
    ) -> Callable[[EffectHandler[State]], EffectHandler[State]]:
        if not event_type:
            raise ValueError("effect event_type must not be empty")

        def register(function: EffectHandler[State]) -> EffectHandler[State]:
            if not inspect.iscoroutinefunction(function):
                raise TypeError("effect handler must be asynchronous")
            if event_type in self._handlers:
                raise ValueError(f"effect already registered for {event_type!r}")
            self._handlers[event_type] = function
            return function

        return register

    def handler_for(self, event_type: str) -> EffectHandler[State] | None:
        return self._handlers.get(event_type)

class AgentDefinition(Generic[State]):
    """One state type, one reducer, and pure synchronous reactions."""

    def __init__(
        self,
        name: str,
        *,
        initial_state: Callable[[], State],
        terminal_event_types: Iterable[str] = (),
        terminal_status_by_event_type: Mapping[str, str] | None = None,
        definition_version: str | None = None,
    ) -> None:
        if not name:
            raise ValueError("agent name must not be empty")
        self.name = name
        self._initial_state = initial_state
        self._terminal_event_types = frozenset(terminal_event_types)
        if any(not event_type for event_type in self._terminal_event_types):
            raise ValueError("terminal event types must not be empty")
        self._terminal_status_by_event_type = dict(terminal_status_by_event_type or {})
        self._definition_version = definition_version
        self._reducer: Reducer[State] | None = None
        self._reactions: dict[str, Reaction[State]] = {}

    @property
    def terminal_event_types(self) -> frozenset[str]:
        return self._terminal_event_types

    @property
    def terminal_status_by_event_type(self) -> Mapping[str, str]:
        return self._terminal_status_by_event_type

    @property
    def definition_version(self) -> str | None:
        return self._definition_version

    def assert_definition_matches(self, history: Sequence[EventEnvelope]) -> None:
        """Raise `DefinitionMismatchError` if `history`'s `RunCreated` event
        was recorded under a different (agent_name, definition_version)
        than this definition. A no-op whenever there is nothing to compare
        -- no `RunCreated` in history (e.g. the non-HTTP flow that starts
        directly with a domain event), or either side has no recorded
        version -- so existing unversioned runs/agents never start failing
        just because this check exists. Every place that interprets or
        continues a run (`rebuild`/`rebuild_through` callers, both durable
        dispatchers, the FastAPI read/stream/command paths) must call this
        first."""
        run_created = next(
            (
                envelope
                for envelope in history
                if envelope.event.event_type == "RunCreated"
            ),
            None,
        )
        if run_created is None:
            return
        recorded_version = run_created.event.data.get("definition_version")
        if recorded_version is None or self._definition_version is None:
            return
        recorded_agent = run_created.event.data.get("agent")
        if (recorded_agent, recorded_version) != (self.name, self._definition_version):
            raise DefinitionMismatchError(
                f"stream {run_created.stream_id!r} was created under "
                f"(agent={recorded_agent!r}, definition_version={recorded_version!r}), "
                f"but the running definition is (agent={self.name!r}, "
                f"definition_version={self._definition_version!r})"
            )

    def reducer(self, function: Reducer[State]) -> Reducer[State]:
        if self._reducer is not None:
            raise ValueError(f"agent {self.name!r} already has a reducer")
        if inspect.iscoroutinefunction(function):
            raise TypeError("reducer must be synchronous")
        self._reducer = function
        return function

    def react(
        self,
        event_type: str,
    ) -> Callable[[Reaction[State]], Reaction[State]]:
        if not event_type:
            raise ValueError("reaction event_type must not be empty")

        def register(function: Reaction[State]) -> Reaction[State]:
            if inspect.iscoroutinefunction(function):
                raise TypeError("reaction must be synchronous and perform no I/O")
            if event_type in self._reactions:
                raise ValueError(
                    f"agent {self.name!r} already reacts to {event_type!r}"
                )
            self._reactions[event_type] = function
            return function

        return register

    def rebuild(
        self,
        history: Sequence[EventEnvelope],
    ) -> State:
        if self._reducer is None:
            raise RuntimeError(f"agent {self.name!r} has no reducer")
        state = self._initial_state()
        for envelope in history:
            state = self._reducer(state, envelope.event)
        return state

    def rebuild_through(
        self,
        history: Sequence[EventEnvelope],
        *,
        through_version: int,
    ) -> State:
        if self._reducer is None:
            raise RuntimeError(f"agent {self.name!r} has no reducer")
        state = self._initial_state()
        for envelope in history:
            if envelope.stream_version > through_version:
                break
            state = self._reducer(state, envelope.event)
        return state

    def evaluate_reaction(
        self,
        event: Event,
        state: State,
    ) -> tuple[Event, ...]:
        reaction = self._reactions.get(event.event_type)
        if reaction is None:
            return ()
        outputs = tuple(reaction(event, state))
        if not all(isinstance(output, Event) for output in outputs):
            raise TypeError("reaction must return only Event values")
        return outputs

    def is_terminal(
        self,
        history: Sequence[EventEnvelope],
        *,
        through_version: int,
    ) -> bool:
        return any(
            envelope.stream_version <= through_version
            and envelope.event.event_type in self._terminal_event_types
            for envelope in history
        )


class DurableDispatcher(Generic[State]):
    """Processes one global event with an atomic output/checkpoint commit."""

    def __init__(
        self,
        *,
        agent: AgentDefinition[State],
        store: EventStore,
        subscription_name: str,
        owns_stream: StreamOwnership | None = None,
    ) -> None:
        if not subscription_name:
            raise ValueError("subscription_name must not be empty")
        self._agent = agent
        self._store = store
        self._subscription_name = subscription_name
        self._owns_stream = owns_stream or partial(
            agent_owns_stream,
            agent.name,
        )

    async def run_once(self) -> bool:
        checkpoint = await self._store.load_checkpoint(self._subscription_name)
        pending = await self._store.load_global(
            after_position=checkpoint,
            limit=1,
        )
        if not pending:
            return False

        consumed = pending[0]
        if not self._owns_stream(consumed.stream_id):
            await _advance_checkpoint_without_outputs(
                store=self._store,
                subscription_name=self._subscription_name,
                checkpoint=checkpoint,
                consumed=consumed,
            )
            return True
        history = await self._store.load(consumed.stream_id)
        try:
            self._agent.assert_definition_matches(history)
        except DefinitionMismatchError as error:
            logger.warning(
                "agentlog: skipping stream %r -- %s (blocked; not "
                "reprocessed automatically -- see DefinitionMismatchError)",
                consumed.stream_id,
                error,
            )
            await _advance_checkpoint_without_outputs(
                store=self._store,
                subscription_name=self._subscription_name,
                checkpoint=checkpoint,
                consumed=consumed,
            )
            return True
        state = self._agent.rebuild_through(
            history,
            through_version=consumed.stream_version,
        )
        outputs = (
            ()
            if self._agent.is_terminal(
                history,
                through_version=consumed.stream_version,
            )
            else self._agent.evaluate_reaction(consumed.event, state)
        )
        _validate_at_most_one_terminal_output(self._agent, outputs)
        await _commit_outputs_with_retry(
            store=self._store,
            subscription_name=self._subscription_name,
            stream_id=consumed.stream_id,
            consumed=consumed,
            outputs=outputs,
            agent=self._agent,
        )
        return True


class DurableEffectDispatcher(Generic[State]):
    """Runs external I/O at least once, then atomically stores results and cursor."""

    def __init__(
        self,
        *,
        agent: AgentDefinition[State],
        store: EventStore,
        effects: EffectRegistry[State],
        context: EffectContext,
        subscription_name: str,
        owns_stream: StreamOwnership | None = None,
    ) -> None:
        if not subscription_name:
            raise ValueError("subscription_name must not be empty")
        self._agent = agent
        self._store = store
        self._effects = effects
        self._context = context
        self._subscription_name = subscription_name
        self._owns_stream = owns_stream or partial(
            agent_owns_stream,
            agent.name,
        )

    async def run_once(self) -> bool:
        checkpoint = await self._store.load_checkpoint(self._subscription_name)
        pending = await self._store.load_global(
            after_position=checkpoint,
            limit=1,
        )
        if not pending:
            return False

        consumed = pending[0]
        if not self._owns_stream(consumed.stream_id):
            await _advance_checkpoint_without_outputs(
                store=self._store,
                subscription_name=self._subscription_name,
                checkpoint=checkpoint,
                consumed=consumed,
            )
            return True
        history_at_dispatch = await self._store.load(consumed.stream_id)
        try:
            self._agent.assert_definition_matches(history_at_dispatch)
        except DefinitionMismatchError as error:
            logger.warning(
                "agentlog: skipping stream %r -- %s (blocked; not "
                "reprocessed automatically -- see DefinitionMismatchError)",
                consumed.stream_id,
                error,
            )
            await _advance_checkpoint_without_outputs(
                store=self._store,
                subscription_name=self._subscription_name,
                checkpoint=checkpoint,
                consumed=consumed,
            )
            return True
        state = self._agent.rebuild_through(
            history_at_dispatch,
            through_version=consumed.stream_version,
        )
        handler = (
            None
            if self._agent.is_terminal(
                history_at_dispatch,
                through_version=consumed.stream_version,
            )
            else self._effects.handler_for(consumed.event.event_type)
        )
        if handler is None:
            outputs: tuple[Event, ...] = ()
        else:
            operation_id = _validate_effect_request(consumed.event)
            raw_outputs = tuple(
                await handler(consumed.event, state, self._context)
            )
            if not all(isinstance(output, Event) for output in raw_outputs):
                raise TypeError("effect handler must return only Event values")
            outputs = _normalize_effect_outputs(
                consumed.event,
                raw_outputs,
                operation_id=operation_id,
            )
        if not all(isinstance(output, Event) for output in outputs):
            raise TypeError("effect handler must return only Event values")
        _validate_at_most_one_terminal_output(self._agent, outputs)
        await _commit_outputs_with_retry(
            store=self._store,
            subscription_name=self._subscription_name,
            stream_id=consumed.stream_id,
            consumed=consumed,
            outputs=outputs,
            agent=self._agent,
        )
        return True
