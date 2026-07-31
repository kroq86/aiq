from __future__ import annotations

from collections.abc import Sequence

from tests.model.reference import NormalizedEvent

MODEL_BOUND = 10
STATE_SIZE = 48

EVENT_TYPES = {
    "RunCreated": 1,
    "UserMessageAdded": 2,
    "ModelCallRequested": 3,
    "ModelCallSucceeded:answer": 4,
    "ModelCallSucceeded:tool": 5,
    "ModelCallFailed": 6,
    "ToolCallRequested": 7,
    "ToolCallSucceeded": 8,
    "ToolCallFailed": 9,
    "AnswerProduced": 10,
    "RunCompleted": 11,
    "RunFailed": 12,
    "ModelLoopLimitExceeded": 13,
}


def _position(identity: str | None) -> int:
    if identity is None:
        return 0
    if not identity.startswith("e"):
        raise ValueError(f"unsupported normalized identity: {identity!r}")
    return int(identity[1:])


def _event_type(event: NormalizedEvent) -> int:
    if event.event_type == "ModelCallSucceeded":
        return EVENT_TYPES[f"ModelCallSucceeded:{dict(event.payload)['kind']}"]
    try:
        return EVENT_TYPES[event.event_type]
    except KeyError as error:
        raise ValueError(f"event is outside formal abstraction: {event.event_type}") from error


def _flags(event: NormalizedEvent, history: Sequence[NormalizedEvent]) -> int:
    payload = dict(event.payload)
    if "model_step" in payload:
        model_step = int(payload["model_step"])
        tool_calls_used = int(payload.get("tool_calls_used", 0))
        if not 0 <= model_step <= 0x0F:
            raise ValueError(f"model_step is outside formal encoding: {model_step}")
        if not 0 <= tool_calls_used <= 0x0F:
            raise ValueError(
                f"tool_calls_used is outside formal encoding: {tool_calls_used}"
            )
        return model_step | (tool_calls_used << 4)
    if event.event_type in {"ModelCallFailed", "ToolCallFailed"} and event.causation:
        return _flags(history[_position(event.causation) - 1], history)
    return 0


def encode_runtime_state(
    history: Sequence[NormalizedEvent],
    checkpoints: tuple[int, int],
) -> str:
    if len(history) > MODEL_BOUND:
        raise ValueError(f"runtime history exceeds formal bound: {len(history)} > {MODEL_BOUND}")
    reaction_checkpoint, effect_checkpoint = checkpoints
    if reaction_checkpoint > len(history) or effect_checkpoint > len(history):
        raise ValueError("runtime checkpoint points past history")

    state = bytearray(STATE_SIZE)
    state[0] = len(history)
    state[1] = reaction_checkpoint
    state[2] = effect_checkpoint
    terminals = {event.event_type for event in history}
    state[3] = 2 if "RunCompleted" in terminals else 3 if "RunFailed" in terminals else 1 if history else 0
    state[4] = 1 if history else 0
    state[5] = sum(
        event.event_type in {"ModelCallSucceeded", "ModelCallFailed"}
        for event in history
    )
    state[6] = sum(
        event.event_type in {"ToolCallSucceeded", "ToolCallFailed"}
        for event in history
    )
    state[7] = 1 if history else 0

    for index, event in enumerate(history):
        offset = 8 + index * 4
        state[offset] = _event_type(event)
        state[offset + 1] = _position(event.causation)
        state[offset + 2] = _position(event.operation)
        state[offset + 3] = _flags(event, history)
    return "x" + state.hex()
