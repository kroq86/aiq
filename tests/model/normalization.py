from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentlog import EventEnvelope

from .reference import NormalizedEvent


def _payload(event_type: str, data: Mapping) -> tuple[tuple[str, object], ...]:
    if event_type == "RunCreated":
        return (("definition_version", data.get("definition_version")),)
    if event_type == "UserMessageAdded":
        return (("text", data["text"]),)
    if event_type == "ModelCallRequested":
        return (
            ("model_step", data["model_step"]),
            ("tool_calls_used", data["tool_calls_used"]),
        )
    if event_type == "ModelCallSucceeded":
        response = data["response"]
        continuation = data["continuation"]
        return (
            ("kind", "tool" if response["tool_calls"] else "answer"),
            ("model_step", continuation["model_step"]),
            ("tool_calls_used", continuation["tool_calls_used"]),
        )
    if event_type == "ToolCallRequested":
        continuation = data["continuation"]
        return (
            ("name", data["call"]["name"]),
            ("model_step", continuation["model_step"]),
            ("tool_calls_used", continuation["tool_calls_used"]),
        )
    if event_type == "ToolCallSucceeded":
        continuation = data["continuation"]
        return (
            ("name", data["name"]),
            ("model_step", continuation["model_step"]),
            ("tool_calls_used", continuation["tool_calls_used"]),
        )
    if event_type == "ToolValidationSucceeded":
        return (("phase", data["phase"]),)
    if event_type == "ToolValidationFailed":
        continuation = data["continuation"]
        return (
            ("model_step", continuation["model_step"]),
            ("tool_calls_used", continuation["tool_calls_used"]),
            ("phase", data["phase"]),
            ("retryable", data["retryable"]),
        )
    if event_type == "AnswerProduced":
        return (("answer", data["answer"]),)
    return ()


def normalize_history(history: Sequence[EventEnvelope]) -> tuple[NormalizedEvent, ...]:
    identities = {
        str(envelope.event.event_id): f"e{index}"
        for index, envelope in enumerate(history, start=1)
    }
    normalized = []
    for index, envelope in enumerate(history, start=1):
        metadata = envelope.event.metadata
        cause = metadata.get("causation_id")
        operation = metadata.get("operation_id")
        normalized.append(
            NormalizedEvent(
                envelope.event.event_type,
                f"e{index}",
                causation=identities.get(str(cause)) if cause is not None else None,
                operation=(
                    identities.get(str(operation)) if operation is not None else None
                ),
                payload=_payload(envelope.event.event_type, envelope.event.data),
            )
        )
    return tuple(normalized)
