"""Canonical Agentlog 0.2 transition specification.

This module is product-independent: it imports neither Agentlog nor transport,
storage, provider, or tool adapters. Tests consume it; they do not define it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


TERMINAL_EVENTS = {"RunCompleted", "RunFailed", "RunAbstained"}


@dataclass(frozen=True)
class NormalizedEvent:
    event_type: str
    identity: str
    causation: str | None = None
    operation: str | None = None
    payload: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class ReferenceState:
    history: tuple[NormalizedEvent, ...]
    reaction_checkpoint: int = 0
    effect_checkpoint: int = 0

    @property
    def terminal(self) -> bool:
        return any(event.event_type in TERMINAL_EVENTS for event in self.history)

    @property
    def answer(self) -> str | None:
        for event in reversed(self.history):
            if event.event_type == "AnswerProduced":
                return dict(event.payload)["answer"]
        return None


def initial_state() -> ReferenceState:
    return ReferenceState(
        history=(
            NormalizedEvent(
                "RunCreated", "e1", payload=(("definition_version", "1"),)
            ),
            NormalizedEvent("UserMessageAdded", "e2", payload=(("text", "weather"),)),
        )
    )


def _append(
    state: ReferenceState,
    event_type: str,
    *,
    cause: str | None = None,
    operation: str | None = None,
    request: bool = False,
    payload: tuple[tuple[str, object], ...] = (),
) -> ReferenceState:
    identity = f"e{len(state.history) + 1}"
    event = NormalizedEvent(
        event_type,
        identity,
        causation=cause,
        operation=identity if request else operation,
        payload=payload,
    )
    return replace(state, history=state.history + (event,))


def _terminal_in_prefix(state: ReferenceState, position: int) -> bool:
    return any(
        event.event_type in TERMINAL_EVENTS
        for event in state.history[:position]
    )


def dispatch_reaction(state: ReferenceState) -> ReferenceState:
    if state.reaction_checkpoint >= len(state.history):
        return state
    position = state.reaction_checkpoint + 1
    consumed = state.history[position - 1]
    next_state = state
    if not _terminal_in_prefix(state, position) and not state.terminal:
        if consumed.event_type == "UserMessageAdded":
            next_state = _append(
                next_state,
                "ModelCallRequested",
                cause=consumed.identity,
                request=True,
                payload=(("model_step", 1), ("tool_calls_used", 0)),
            )
        elif consumed.event_type == "ModelCallSucceeded":
            data = dict(consumed.payload)
            if data["kind"] == "tool":
                next_state = _append(
                    next_state,
                    "ToolCallRequested",
                    cause=consumed.identity,
                    request=True,
                    payload=(
                        ("name", "get_weather"),
                        ("model_step", data["model_step"]),
                        ("tool_calls_used", data["tool_calls_used"] + 1),
                    ),
                )
            else:
                next_state = _append(
                    next_state,
                    "AnswerProduced",
                    cause=consumed.identity,
                    payload=(("answer", "23 C"),),
                )
                next_state = _append(
                    next_state, "RunCompleted", cause=consumed.identity
                )
        elif consumed.event_type == "ToolCallSucceeded":
            data = dict(consumed.payload)
            next_state = _append(
                next_state,
                "ModelCallRequested",
                cause=consumed.identity,
                request=True,
                payload=(
                    ("model_step", data["model_step"] + 1),
                    ("tool_calls_used", data["tool_calls_used"]),
                ),
            )
        elif consumed.event_type == "ToolValidationFailed":
            data = dict(consumed.payload)
            if data["retryable"]:
                next_state = _append(
                    next_state,
                    "ModelCallRequested",
                    cause=consumed.identity,
                    request=True,
                    payload=(
                        ("model_step", data["model_step"] + 1),
                        ("tool_calls_used", data["tool_calls_used"]),
                    ),
                )
            else:
                next_state = _append(next_state, "RunFailed", cause=consumed.identity)
        elif consumed.event_type in {
            "ModelCallRejected",
            "ModelCallFailed",
            "ModelOutputRejected",
            "ToolCallRejected",
            "ToolCallFailed",
            "ModelLoopLimitExceeded",
        }:
            next_state = _append(next_state, "RunFailed", cause=consumed.identity)
    return replace(next_state, reaction_checkpoint=position)


def dispatch_effect(
    state: ReferenceState,
    *,
    model_failure: bool = False,
    tool_failure: bool = False,
    validation_outcome: str | None = None,
) -> ReferenceState:
    if state.effect_checkpoint >= len(state.history):
        return state
    position = state.effect_checkpoint + 1
    consumed = state.history[position - 1]
    next_state = state
    if not _terminal_in_prefix(state, position) and not state.terminal:
        if consumed.event_type == "ModelCallRequested":
            data = dict(consumed.payload)
            if model_failure:
                next_state = _append(
                    next_state,
                    "ModelCallFailed",
                    cause=consumed.identity,
                    operation=consumed.operation,
                )
            else:
                kind = "tool" if data["model_step"] == 1 else "answer"
                next_state = _append(
                    next_state,
                    "ModelCallSucceeded",
                    cause=consumed.identity,
                    operation=consumed.operation,
                    payload=(
                        ("kind", kind),
                        ("model_step", data["model_step"]),
                        ("tool_calls_used", data["tool_calls_used"]),
                    ),
                )
        elif consumed.event_type == "ToolCallRequested":
            data = dict(consumed.payload)
            validation_payload = (
                ("model_step", data["model_step"]),
                ("tool_calls_used", data["tool_calls_used"]),
            )
            if validation_outcome in {"reject", "retry", "ambiguous"}:
                next_state = _append(
                    next_state,
                    "ToolValidationFailed",
                    cause=consumed.identity,
                    operation=consumed.operation,
                    payload=validation_payload
                    + (
                        ("phase", "request"),
                        ("retryable", validation_outcome != "reject"),
                    ),
                )
            elif validation_outcome == "postcondition_failure":
                next_state = _append(
                    next_state,
                    "ToolValidationSucceeded",
                    cause=consumed.identity,
                    operation=consumed.operation,
                    payload=(("phase", "request"),),
                )
                next_state = _append(
                    next_state,
                    "ToolValidationFailed",
                    cause=consumed.identity,
                    operation=consumed.operation,
                    payload=validation_payload
                    + (("phase", "result"), ("retryable", False)),
                )
            elif tool_failure:
                next_state = _append(
                    next_state,
                    "ToolCallFailed",
                    cause=consumed.identity,
                    operation=consumed.operation,
                )
            else:
                if validation_outcome == "accept":
                    next_state = _append(
                        next_state,
                        "ToolValidationSucceeded",
                        cause=consumed.identity,
                        operation=consumed.operation,
                        payload=(("phase", "request"),),
                    )
                    next_state = _append(
                        next_state,
                        "ToolValidationSucceeded",
                        cause=consumed.identity,
                        operation=consumed.operation,
                        payload=(("phase", "result"),),
                    )
                next_state = _append(
                    next_state,
                    "ToolCallSucceeded",
                    cause=consumed.identity,
                    operation=consumed.operation,
                    payload=(
                        ("name", "get_weather"),
                        ("model_step", data["model_step"]),
                        ("tool_calls_used", data["tool_calls_used"]),
                    ),
                )
    return replace(next_state, effect_checkpoint=position)


def step(state: ReferenceState, action: str) -> ReferenceState:
    if action == "reaction":
        return dispatch_reaction(state)
    if action == "effect":
        return dispatch_effect(state)
    if action == "effect_model_failure":
        return dispatch_effect(state, model_failure=True)
    if action == "effect_tool_failure":
        return dispatch_effect(state, tool_failure=True)
    if action.startswith("effect_validation_"):
        return dispatch_effect(
            state, validation_outcome=action.removeprefix("effect_validation_")
        )
    if action == "restart":
        return state
    if action == "force_terminal":
        if state.terminal:
            return state
        return _append(state, "RunFailed")
    raise ValueError(f"unknown reference action: {action!r}")


def assert_invariants(previous: ReferenceState, current: ReferenceState) -> None:
    assert previous.history == current.history[: len(previous.history)]
    assert current.reaction_checkpoint >= previous.reaction_checkpoint
    assert current.effect_checkpoint >= previous.effect_checkpoint
    assert sum(
        event.event_type in TERMINAL_EVENTS for event in current.history
    ) <= 1
    # NOTE(vacuity): no action in this module's `step()`/`dispatch_reaction`/
    # `dispatch_effect` ever emits GoalSatisfied, GoalNotSatisfied, or
    # WorkflowCycleDetected, and no test in this repository constructs a
    # ReferenceState containing them either. The two checks below are
    # therefore currently vacuous for every generated/bounded exploration
    # and every committed test -- they document the intended v0.4 ordering
    # and exclusion properties for when/if actions producing these event
    # types are added to this reference model (formal-model boundary
    # decision B in FORMAL_MODEL.md Sec. 2.1); they are not evidence that
    # either property has been checked against any reachable state.
    event_types = tuple(event.event_type for event in current.history)
    if "GoalNotSatisfied" in event_types or "WorkflowCycleDetected" in event_types:
        assert "RunCompleted" not in event_types
    if "GoalSatisfied" in event_types and "RunCompleted" in event_types:
        assert event_types.index("GoalSatisfied") < event_types.index("RunCompleted")
    assert current.reaction_checkpoint <= len(current.history)
    assert current.effect_checkpoint <= len(current.history)
    identities = {event.identity for event in current.history}
    result_types = {
        "ModelCallSucceeded",
        "ModelCallFailed",
        "ModelCallRejected",
        "ModelOutputRejected",
        "ToolCallSucceeded",
        "ToolCallFailed",
        "ToolCallRejected",
        "ToolValidationFailed",
    }
    results_by_cause: dict[str, int] = {}
    for event in current.history:
        if event.causation is not None:
            assert event.causation in identities
            assert int(event.causation[1:]) < int(event.identity[1:])
        if event.operation is not None:
            assert event.operation in identities
        if event.event_type in result_types:
            assert event.causation is not None
            assert event.operation == event.causation
            results_by_cause[event.causation] = results_by_cause.get(event.causation, 0) + 1
        if event.event_type == "ToolValidationFailed":
            data = dict(event.payload)
            if data["phase"] == "result":
                assert data["retryable"] is False
                assert any(
                    prior.event_type == "ToolValidationSucceeded"
                    and prior.causation == event.causation
                    and dict(prior.payload)["phase"] == "request"
                    and int(prior.identity[1:]) < int(event.identity[1:])
                    for prior in current.history
                )
    assert all(count == 1 for count in results_by_cause.values())
