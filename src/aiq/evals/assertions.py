from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aiq.trace import CausalTrace, TraceEvent

from .case import EvalCase


_TOOL_FAILURE_EVENTS = frozenset({"ToolCallFailed", "ToolCallRejected"})
_OPERATION_REQUEST_EVENTS = frozenset({"ModelCallRequested", "ToolCallRequested"})


@dataclass(frozen=True, slots=True)
class AssertionFailure:
    assertion: str
    expected: object
    actual: object
    message: str


def _tool_name(event: TraceEvent) -> str | None:
    if event.event_type != "ToolCallRequested" or not isinstance(event.data, Mapping):
        return None
    call = event.data.get("call")
    if not isinstance(call, Mapping):
        return None
    name = call.get("name")
    return name if isinstance(name, str) else None


def _operation_id_failures(trace: CausalTrace) -> tuple[AssertionFailure, ...]:
    failures: list[AssertionFailure] = []
    requests = {
        event.event_id: event.operation_id
        for event in trace.events
        if event.event_type in _OPERATION_REQUEST_EVENTS
    }
    for event in trace.events:
        if event.event_type in _OPERATION_REQUEST_EVENTS:
            if event.operation_id != event.event_id:
                failures.append(
                    AssertionFailure(
                        "stable_operation_ids",
                        event.event_id,
                        event.operation_id,
                        f"{event.event_type} operation_id must equal its event_id",
                    )
                )
            continue
        if event.causation_id not in requests:
            continue
        expected = requests[event.causation_id]
        if event.operation_id != expected:
            failures.append(
                AssertionFailure(
                    "stable_operation_ids",
                    expected,
                    event.operation_id,
                    f"{event.event_type} changed its request operation_id",
                )
            )
    return tuple(failures)


def evaluate_trace(case: EvalCase, trace: CausalTrace) -> tuple[AssertionFailure, ...]:
    failures: list[AssertionFailure] = []

    actual_tools = tuple(
        name for event in trace.events if (name := _tool_name(event)) is not None
    )
    if actual_tools != case.expected_tools:
        failures.append(
            AssertionFailure(
                "expected_tools",
                case.expected_tools,
                actual_tools,
                "tool-call trajectory did not match",
            )
        )

    if case.expected_terminal is not None and trace.terminal_status != case.expected_terminal:
        failures.append(
            AssertionFailure(
                "expected_terminal",
                case.expected_terminal,
                trace.terminal_status,
                "terminal status did not match",
            )
        )

    model_steps = sum(
        event.event_type == "ModelCallRequested" for event in trace.events
    )
    if case.max_model_steps is not None and model_steps > case.max_model_steps:
        failures.append(
            AssertionFailure(
                "max_model_steps",
                case.max_model_steps,
                model_steps,
                "model-step limit was exceeded",
            )
        )

    if case.assertions.no_tool_failure:
        tool_failures = tuple(
            event.event_type
            for event in trace.events
            if event.event_type in _TOOL_FAILURE_EVENTS
        )
        if tool_failures:
            failures.append(
                AssertionFailure(
                    "no_tool_failure",
                    (),
                    tool_failures,
                    "tool failure events were committed",
                )
            )

    if case.assertions.stable_operation_ids:
        failures.extend(_operation_id_failures(trace))

    return tuple(failures)
