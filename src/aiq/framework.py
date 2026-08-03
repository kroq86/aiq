"""Declarative framework API on top of the existing AIQ core.

`Agent` lets a host declare event types, reducers, reactions, effects and
commands without hand-assembling `AgentDefinition`, `EffectRegistry` and
`EffectContext`. `Agent.build_runtime()` compiles the declaration into
exactly those existing classes -- this module is not a second execution
engine, it is a builder for the one that already exists in
`aiq.runtime`.

    from aiq.framework import Agent

    agent = Agent(name="assistant", initial_state=ChatState())

    @agent.event
    @dataclass(frozen=True)
    class UserMessageAdded:
        text: str

    @agent.reduce(UserMessageAdded)
    def on_user_message(state, event):
        return replace(state, messages=state.messages + (event.text,))

    @agent.react(UserMessageAdded)
    def request_model(state, event):
        return ModelCallRequested(prompt=event.text)

    @agent.effect(ModelCallRequested)
    async def call_model(effect, context):
        result = await context.model.complete(effect.prompt)
        return ModelCallSucceeded(operation_id=effect.operation_id, text=result)

    runtime = agent.build_runtime(context=context)

`runtime` is a plain `aiq.application.AgentRuntime` -- pass it into
`AIQ(store=store, runtimes={agent.name: runtime})` exactly as if it
had been hand-assembled.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol, TypeVar

from .application import AgentRuntime, default_serialize_state
from .core import Event, JsonValue
from .runtime import AgentDefinition, EffectContext, EffectRegistry, effect_request

State = TypeVar("State")

__all__ = ["Agent", "AgentPolicy", "CommandRejected", "DefinitionError", "EffectFailed"]

_TRANSPORT_FIELDS = frozenset({"operation_id", "causation_id"})
_BUILTIN_EVENT_NAMES = frozenset({"CommandRejected", "EffectFailed"})


class CommandRejected(Exception):
    """Raise from a `@agent.command` handler to reject it as an explicit,
    recorded domain fact instead of a bare HTTP error. The framework
    catches only this type; any other exception still propagates
    unchanged (KeyError/TypeError/ValueError keep mapping to HTTP-only
    404/422, anything else is a real bug and surfaces as a 500)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EffectFailed(Exception):
    """Raise from an `@agent.effect` handler to record a durable domain
    failure event instead of tripping the worker's process-wide
    circuit breaker. The framework catches only this type; any other
    exception still propagates and marks the worker unhealthy, exactly
    as before -- that stays the safety net for real bugs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DefinitionError(ValueError):
    """A declarative agent definition contains conflicting declarations."""


class AgentPolicy(Protocol):
    name: str

    def install(self, agent: "Agent") -> None: ...


@dataclasses.dataclass(frozen=True)
class _CommandRejectedEvent:
    reason: str


_CommandRejectedEvent.__name__ = "CommandRejected"
_CommandRejectedEvent.__qualname__ = "CommandRejected"


@dataclasses.dataclass(frozen=True)
class _EffectFailedEvent:
    reason: str


_EffectFailedEvent.__name__ = "EffectFailed"
_EffectFailedEvent.__qualname__ = "EffectFailed"


def _require_dataclass(cls: type, *, what: str) -> None:
    if not isinstance(cls, type):
        raise TypeError(f"{what} must be a class, got {cls!r}")
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{what} {cls.__name__!r} must be a dataclass")


def _check_arity(fn: Callable[..., Any], expected: int, *, label: str) -> None:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return  # not introspectable (e.g. some builtins) -- don't block on it
    required = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        and parameter.default is parameter.empty
    ]
    if len(required) != expected:
        raise TypeError(
            f"{label} must accept exactly {expected} positional argument(s), "
            f"got {len(required)}"
        )


class Agent:
    """Holds an agent's declaration until `build_runtime()` compiles it.

    Registration methods (`event`/`reduce`/`react`/`effect`/`command`)
    validate and raise immediately -- declaration errors surface at
    decoration time, not later when the runtime is exercised.
    """

    def __init__(
        self,
        *,
        name: str,
        initial_state: State | Callable[[], State],
        serialize_state: Callable[[State], JsonValue] | None = None,
        version: str | None = None,
    ) -> None:
        if not name:
            raise ValueError("Agent name must not be empty")
        self.name = name
        self._initial_state_factory: Callable[[], State] = (
            initial_state if callable(initial_state) else (lambda value=initial_state: value)
        )
        self._serialize_state = serialize_state
        self._version = version

        self._event_types: dict[str, type] = {
            "CommandRejected": _CommandRejectedEvent,
            "EffectFailed": _EffectFailedEvent,
        }
        self._reducers: dict[str, Callable[[Any, Any], Any]] = {}
        self._reactions: dict[str, Callable[[Any, Any], Any]] = {}
        self._effects: dict[str, Callable[[Any, Any], Any]] = {}
        self._commands: dict[str, Callable[[Any], Any]] = {}
        self._terminal_events: dict[str, str] = {}
        self._policy_names: set[str] = set()
        self._resource_validators: list[Callable[[object], None]] = []

    def _claim_policy(
        self,
        name: str,
        *,
        validate_resources: Callable[[object], None] | None = None,
    ) -> None:
        """Reserve a namespace before a policy mutates the declaration."""
        if not name:
            raise DefinitionError("policy name must not be empty")
        if name in self._policy_names:
            raise DefinitionError(f"policy namespace already installed: {name!r}")
        self._policy_names.add(name)
        if validate_resources is not None:
            self._resource_validators.append(validate_resources)

    # -- declaration -------------------------------------------------

    def event(self, cls: type) -> type:
        """Register an immutable event type. Idempotent for the exact same
        class; a *different* class reusing the same name is rejected."""
        _require_dataclass(cls, what="@agent.event target")
        event_name = cls.__name__
        existing = self._event_types.get(event_name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"event name {event_name!r} is already registered to a "
                f"different class ({existing!r})"
            )
        self._event_types[event_name] = cls
        return cls

    def _registered_event_name(self, event_type: type, *, decorator: str) -> str:
        _require_dataclass(event_type, what=f"@agent.{decorator} target")
        event_name = event_type.__name__
        if self._event_types.get(event_name) is not event_type:
            raise ValueError(
                f"{event_type.__name__!r} is not registered -- decorate it "
                "with @agent.event before @agent."
                f"{decorator}"
            )
        return event_name

    def reduce(
        self, event_type: type
    ) -> Callable[[Callable[[Any, Any], Any]], Callable[[Any, Any], Any]]:
        """Register `(state, event) -> state` for `event_type`. Exactly one
        reducer per event type; unhandled event types leave state unchanged."""
        event_name = self._registered_event_name(event_type, decorator="reduce")

        def register(fn: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
            if event_name in self._reducers:
                raise ValueError(f"duplicate reducer for {event_name!r}")
            _check_arity(fn, 2, label=f"reducer for {event_name!r}")
            self._reducers[event_name] = fn
            return fn

        return register

    def react(
        self, event_type: type
    ) -> Callable[[Callable[[Any, Any], Any]], Callable[[Any, Any], Any]]:
        """Register `(state, event) -> Event | Iterable[Event] | None` for
        `event_type`. Must be synchronous and side-effect free, matching
        the underlying core's reaction contract."""
        event_name = self._registered_event_name(event_type, decorator="react")

        def register(fn: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
            if event_name in self._reactions:
                raise ValueError(f"duplicate reaction for {event_name!r}")
            if inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"reaction for {event_name!r} must be synchronous and "
                    "perform no I/O"
                )
            _check_arity(fn, 2, label=f"reaction for {event_name!r}")
            self._reactions[event_name] = fn
            return fn

        return register

    def effect(
        self, event_type: type
    ) -> Callable[[Callable[[Any, Any], Any]], Callable[[Any, Any], Any]]:
        """Register an async durable-effect handler `(effect, context) ->
        Event | Iterable[Event]` for the request type `event_type`. Runs
        only through the existing `DurableEffectDispatcher` -- never
        called directly from an HTTP request."""
        event_name = self._registered_event_name(event_type, decorator="effect")

        def register(fn: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
            if event_name in self._effects:
                raise ValueError(f"duplicate effect handler for {event_name!r}")
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"effect handler for {event_name!r} must be async "
                    "(sync functions cannot be registered as effects)"
                )
            _check_arity(fn, 2, label=f"effect handler for {event_name!r}")
            self._effects[event_name] = fn
            return fn

        return register

    def command(
        self, name_or_type: str | type
    ) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        """Register a command handler `(payload) -> Event | Iterable[Event]
        | None`, invoked later via `handle_command()`. Accepts either a
        plain string name or a payload type (whose `__name__` is used as
        the command name) -- deliberately not a full command-bus: this is
        just the seam a host adapter (e.g. an HTTP endpoint) uses to turn
        an incoming request into the first domain event(s), without
        reaching into runtime internals."""
        command_name = name_or_type if isinstance(name_or_type, str) else name_or_type.__name__
        if not command_name:
            raise ValueError("command name must not be empty")

        def register(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
            if command_name in self._commands:
                raise ValueError(f"duplicate command {command_name!r}")
            _check_arity(fn, 1, label=f"command handler {command_name!r}")
            self._commands[command_name] = fn
            return fn

        return register

    def event_type(self, name: str) -> type:
        """Look up an already-registered event class by name -- including
        the framework's built-in `CommandRejected`/`EffectFailed`, so a
        reducer/reaction can be attached to them (e.g. to produce a
        terminal `RunFailed` event) without redeclaring them."""
        try:
            return self._event_types[name]
        except KeyError as error:
            raise KeyError(f"event type not registered: {name!r}") from error

    def terminal(self, event_type: type, *, status: str) -> None:
        """Mark `event_type` as ending a run with `status` (any string the
        host chooses, e.g. "completed"/"failed"/"cancelled"). Threaded
        into `AgentDefinition.terminal_event_types`/
        `terminal_status_by_event_type` at `build_runtime()` time -- once
        marked, the underlying runtime stops dispatching reactions/effects
        for the run, `POST .../commands/...` rejects with 409, and the SSE
        stream closes."""
        if not status:
            raise ValueError("terminal status must not be empty")
        event_name = self._registered_event_name(event_type, decorator="terminal")
        if event_name in self._terminal_events:
            raise ValueError(f"{event_name!r} is already marked terminal")
        self._terminal_events[event_name] = status

    # -- command execution --------------------------------------------

    def handle_command(self, command_name: str, payload: Any = None) -> tuple[Event, ...]:
        """Turn one incoming command into its produced domain event(s),
        as raw core `Event` objects ready to append to an event store.
        Does not touch the store or any runtime itself -- a host adapter
        (e.g. a FastAPI route) is expected to call this and then append
        the result.

        A `CommandRejected` raised by the handler is caught here and
        turned into a `CommandRejected` domain event rather than
        propagating -- every other exception is left untouched."""
        handler = self._commands.get(command_name)
        if handler is None:
            raise KeyError(f"unknown command: {command_name!r}")
        try:
            result = handler(payload)
        except CommandRejected as error:
            return self._coerce_events(
                _CommandRejectedEvent(reason=error.reason), causation_source=None
            )
        return self._coerce_events(result, causation_source=None)

    # -- compilation ----------------------------------------------------

    def build_runtime(self, *, context: object = None) -> AgentRuntime:
        """Compile this declaration into a plain `aiq.application.AgentRuntime`,
        using the existing `AgentDefinition`/`EffectRegistry`/`EffectContext`
        as the one and only execution path."""
        for validate_resources in self._resource_validators:
            validate_resources(context)
        if self._event_types.keys() <= _BUILTIN_EVENT_NAMES:
            raise ValueError(
                f"agent {self.name!r} has no registered event types "
                "(use @agent.event before build_runtime())"
            )

        agent_definition: AgentDefinition[Any] = AgentDefinition(
            self.name,
            initial_state=self._initial_state_factory,
            terminal_event_types=self._terminal_events.keys(),
            terminal_status_by_event_type=dict(self._terminal_events),
            definition_version=self._version,
        )

        reducers = dict(self._reducers)
        event_types = dict(self._event_types)

        @agent_definition.reducer
        def dispatch_reducer(state: Any, event: Event) -> Any:
            reducer_fn = reducers.get(event.event_type)
            if reducer_fn is None:
                return state
            typed_event = self._reconstruct(event_types[event.event_type], event)
            return reducer_fn(state, typed_event)

        for event_name, reaction_fn in self._reactions.items():
            event_cls = self._event_types[event_name]
            agent_definition.react(event_name)(
                self._make_reaction_adapter(event_cls, reaction_fn)
            )

        effects_registry: EffectRegistry[Any] = EffectRegistry()
        for event_name, effect_fn in self._effects.items():
            event_cls = self._event_types[event_name]
            effects_registry.effect(event_name)(
                self._make_effect_adapter(event_cls, effect_fn, context)
            )

        return AgentRuntime(
            agent=agent_definition,
            effects=effects_registry,
            context=EffectContext({}),
            serialize_state=self._serialize_state or default_serialize_state,
            command_handler=self.handle_command,
            definition_version=self._version,
        )

    # -- adapters between typed declarations and the raw core -----------

    def _make_reaction_adapter(
        self, event_cls: type, reaction_fn: Callable[[Any, Any], Any]
    ) -> Callable[[Event, Any], Sequence[Event]]:
        def adapter(event: Event, state: Any) -> Sequence[Event]:
            typed_event = self._reconstruct(event_cls, event)
            result = reaction_fn(state, typed_event)
            return self._coerce_events(result, causation_source=event)

        return adapter

    def _make_effect_adapter(
        self,
        event_cls: type,
        effect_fn: Callable[[Any, Any], Any],
        context: object,
    ) -> Callable[[Event, Any, EffectContext], Any]:
        async def adapter(event: Event, state: Any, _effect_context: EffectContext) -> Sequence[Event]:
            typed_event = self._reconstruct(event_cls, event)
            try:
                result = await effect_fn(typed_event, context)
            except EffectFailed as error:
                # The underlying DurableEffectDispatcher stamps operation_id
                # and causation_id on every returned result event itself --
                # including this one -- so it commits atomically with the
                # checkpoint exactly like any other effect result.
                return self._coerce_events(
                    _EffectFailedEvent(reason=error.reason), causation_source=None
                )
            # The underlying DurableEffectDispatcher stamps operation_id and
            # causation_id on every returned result event itself; the
            # framework does not need to (and must not) duplicate that.
            return self._coerce_events(result, causation_source=None)

        return adapter

    def _reconstruct(self, event_cls: type, event: Event) -> Any:
        field_names = {field.name for field in dataclasses.fields(event_cls)}
        values = dict(event.data)
        if "operation_id" in field_names:
            operation_id = event.metadata.get("operation_id")
            values["operation_id"] = (
                str(operation_id) if operation_id is not None else str(event.event_id)
            )
        if "causation_id" in field_names:
            causation_id = event.metadata.get("causation_id")
            if causation_id is not None:
                values["causation_id"] = str(causation_id)
        return event_cls(**values)

    def _coerce_events(
        self, result: Any, *, causation_source: Event | None
    ) -> tuple[Event, ...]:
        if result is None:
            instances: tuple[Any, ...] = ()
        elif dataclasses.is_dataclass(result) and not isinstance(result, type):
            instances = (result,)
        elif isinstance(result, Iterable):
            instances = tuple(result)
        else:
            raise TypeError(
                "reaction/effect/command must return None, a dataclass "
                f"event instance, or an iterable of them; got {result!r}"
            )
        return tuple(
            self._to_raw_event(instance, causation_source=causation_source)
            for instance in instances
        )

    def _to_raw_event(self, instance: Any, *, causation_source: Event | None) -> Event:
        if not dataclasses.is_dataclass(instance) or isinstance(instance, type):
            raise TypeError(
                f"produced event must be a dataclass instance, got {instance!r}"
            )
        cls = type(instance)
        event_name = cls.__name__
        if self._event_types.get(event_name) is not cls:
            raise TypeError(
                f"produced event type {event_name!r} is not registered -- "
                "decorate it with @agent.event"
            )
        data = {
            field.name: _event_value(getattr(instance, field.name))
            for field in dataclasses.fields(instance)
            if field.name not in _TRANSPORT_FIELDS
        }
        metadata: dict[str, JsonValue] = {}
        if causation_source is not None:
            metadata["causation_id"] = str(causation_source.event_id)

        if event_name in self._effects:
            return effect_request(event_name, data, metadata)
        return Event(event_name, data, metadata)


def _event_value(value: Any) -> Any:
    """Convert dataclass event fields without deepcopying frozen mappings."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _event_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {key: _event_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_event_value(item) for item in value)
    return value
