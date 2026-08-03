"""A derived, computed-fresh JSON run report over one run's `CausalTrace`.

This is not a new durable mechanism: `build_run_report` reads only the
already-public `CausalTrace`/`TraceEvent` fields (`docs/flow-xray.md`'s
"causal trace export" contract) and computes counts/latencies from them. It
adds nothing to the event log, changes no reducer/reaction/effect semantics,
and is safe to call for any `DurableModelLoop`-based agent. The default event
contract is the default ``namespace="model"``; callers using another
namespace must pass that loop's ``loop.events`` to `build_run_report`.
Fields that don't apply to a given run (no goal policy configured, no tool
calls made) report `None`/empty rather than guessing.

Latency is wall-clock `created_at` delta between a request event and the
events it causally produced (`causation_id`), not internal handler
instrumentation. Validation hooks (`validate_input`/`validate_transition`/
`capture_pre_state`/`validate_output`) and the tool call itself are
committed together in one effect-output batch, so their `created_at` values
coincide; this report therefore reports one `tool_call_seconds` bucket
(request -> committed outcome, whatever hooks ran inside), not a
per-hook/per-phase breakdown. Do not read `tool_call_seconds` as tool
execution time alone.

The `loop_events`-mismatch guard below only detects a *namespaced*
`DurableModelLoop` contract that does not match the trace. It does not
detect a trace that contains no `DurableModelLoop` events at all (e.g. a
plain `Agent` or a `Sequence` child run passed in by mistake with
`loop_events=None`): `build_run_report` will return a report where every
model-loop-specific field is zero/`None`/empty rather than raising. Check
`report.event_type_counts` if you need to confirm the trace actually came
from a `DurableModelLoop` agent.

This report also does not observe physical-retry/crash-window activity
(`docs/model-loop.md`'s crash window): a provider call retried after a
crash but before its result was committed leaves no separate trace event,
so `model_step_count`/`tool_call_count` reflect committed outcomes only, not
physical invocation counts.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .model_loop import ModelLoopEvents
from .trace import CausalTrace, TraceEvent


@dataclass(frozen=True, slots=True)
class _ModelLoopEventNames:
    model_call_requested: str
    tool_call_requested: str
    tool_call_succeeded: str
    tool_call_failed: str
    tool_call_rejected: str
    tool_validation_failed: str
    goal_satisfied: str
    goal_not_satisfied: str
    workflow_invariant_violated: str
    workflow_cycle_detected: str
    run_abstained: str

    @classmethod
    def from_loop_events(cls, events: ModelLoopEvents) -> _ModelLoopEventNames:
        return cls(
            model_call_requested=events.ModelCallRequested.__name__,
            tool_call_requested=events.ToolCallRequested.__name__,
            tool_call_succeeded=events.ToolCallSucceeded.__name__,
            tool_call_failed=events.ToolCallFailed.__name__,
            tool_call_rejected=events.ToolCallRejected.__name__,
            tool_validation_failed=events.ToolValidationFailed.__name__,
            goal_satisfied=events.GoalSatisfied.__name__,
            goal_not_satisfied=events.GoalNotSatisfied.__name__,
            workflow_invariant_violated=events.WorkflowInvariantViolated.__name__,
            workflow_cycle_detected=events.WorkflowCycleDetected.__name__,
            run_abstained=events.RunAbstained.__name__,
        )

    def all(self) -> frozenset[str]:
        return frozenset(
            (
                self.model_call_requested,
                self.tool_call_requested,
                self.tool_call_succeeded,
                self.tool_call_failed,
                self.tool_call_rejected,
                self.tool_validation_failed,
                self.goal_satisfied,
                self.goal_not_satisfied,
                self.workflow_invariant_violated,
                self.workflow_cycle_detected,
                self.run_abstained,
            )
        )


_DEFAULT_EVENT_NAMES = _ModelLoopEventNames(
    model_call_requested="ModelCallRequested",
    tool_call_requested="ToolCallRequested",
    tool_call_succeeded="ToolCallSucceeded",
    tool_call_failed="ToolCallFailed",
    tool_call_rejected="ToolCallRejected",
    tool_validation_failed="ToolValidationFailed",
    goal_satisfied="GoalSatisfied",
    goal_not_satisfied="GoalNotSatisfied",
    workflow_invariant_violated="WorkflowInvariantViolated",
    workflow_cycle_detected="WorkflowCycleDetected",
    run_abstained="RunAbstained",
)


@dataclass(frozen=True, slots=True)
class RunReport:
    agent_name: str
    run_id: str
    terminal_status: str
    event_count: int
    event_type_counts: dict[str, int]
    model_step_count: int
    tool_call_count: int
    tool_call_succeeded_count: int
    tool_call_failed_count: int
    tool_call_rejected_count: int
    validation_retry_count: int
    validation_non_retryable_failure_count: int
    goal_policy_observed: bool
    goal_satisfied: bool | None
    workflow_invariant_violated: bool
    workflow_cycle_detected: bool
    abstained: bool
    model_latency_seconds: tuple[float, ...]
    tool_call_latency_seconds: tuple[float, ...]


def _parse(created_at: str) -> datetime:
    return datetime.fromisoformat(created_at)


def _latencies(
    events: tuple[TraceEvent, ...],
    request_types: set[str],
    outcome_types: set[str] | None,
) -> tuple[float, ...]:
    by_id = {event.event_id: event for event in events}
    samples: list[float] = []
    for event in events:
        if event.causation_id is None:
            continue
        cause = by_id.get(event.causation_id)
        if cause is None or cause.event_type not in request_types:
            continue
        if outcome_types is not None and event.event_type not in outcome_types:
            continue
        seconds = (_parse(event.created_at) - _parse(cause.created_at)).total_seconds()
        if seconds >= 0:
            samples.append(seconds)
    return tuple(samples)


def _event_names(
    trace: CausalTrace, loop_events: ModelLoopEvents | None
) -> _ModelLoopEventNames:
    trace_types = {event.event_type for event in trace.events}
    # Do not use suffix matching to attribute events. It is only a misuse
    # guard: a trace containing namespaced model-loop events must not silently
    # produce a plausible all-zero report under the wrong event contract.
    default_names = _DEFAULT_EVENT_NAMES.all()
    if loop_events is None and trace_types.intersection(default_names):
        return _DEFAULT_EVENT_NAMES
    namespaced_candidates = sorted(
        event_type
        for event_type in trace_types
        if event_type not in default_names
        and any(event_type.endswith(base_name) for base_name in default_names)
    )
    if loop_events is None:
        if not namespaced_candidates:
            return _DEFAULT_EVENT_NAMES
    else:
        names = _ModelLoopEventNames.from_loop_events(loop_events)
        if trace_types.intersection(names.all()):
            return names

    if namespaced_candidates:
        raise ValueError(
            "model-loop event contract does not match trace; pass the "
            "originating loop.events to build_run_report "
            f"(unmatched events: {namespaced_candidates})"
        )
    return _ModelLoopEventNames.from_loop_events(loop_events)


def build_run_report(
    trace: CausalTrace, *, loop_events: ModelLoopEvents | None = None
) -> RunReport:
    """Pure, computed-fresh report over an already-loaded `CausalTrace`.

    `loop_events` is required when the originating `DurableModelLoop` uses a
    non-default namespace.
    """
    names = _event_names(trace, loop_events)
    counts = Counter(event.event_type for event in trace.events)
    goal_satisfied: bool | None = None
    if counts[names.goal_satisfied]:
        goal_satisfied = True
    elif counts[names.goal_not_satisfied]:
        goal_satisfied = False

    validation_retryable = 0
    validation_non_retryable = 0
    for event in trace.events:
        if event.event_type != names.tool_validation_failed:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        if data.get("retryable"):
            validation_retryable += 1
        else:
            validation_non_retryable += 1

    return RunReport(
        agent_name=trace.agent_name,
        run_id=trace.run_id,
        terminal_status=trace.terminal_status,
        event_count=len(trace.events),
        event_type_counts=dict(counts),
        model_step_count=counts[names.model_call_requested],
        tool_call_count=counts[names.tool_call_requested],
        tool_call_succeeded_count=counts[names.tool_call_succeeded],
        tool_call_failed_count=counts[names.tool_call_failed],
        tool_call_rejected_count=counts[names.tool_call_rejected],
        validation_retry_count=validation_retryable,
        validation_non_retryable_failure_count=validation_non_retryable,
        goal_policy_observed=bool(
            counts[names.goal_satisfied] or counts[names.goal_not_satisfied]
        ),
        goal_satisfied=goal_satisfied,
        workflow_invariant_violated=bool(
            counts[names.workflow_invariant_violated]
        ),
        workflow_cycle_detected=bool(counts[names.workflow_cycle_detected]),
        abstained=bool(counts[names.run_abstained]),
        model_latency_seconds=_latencies(
            trace.events, {names.model_call_requested}, None
        ),
        tool_call_latency_seconds=_latencies(
            trace.events,
            {names.tool_call_requested},
            {
                names.tool_call_succeeded,
                names.tool_call_failed,
                names.tool_call_rejected,
                names.tool_validation_failed,
            },
        ),
    )


def run_report_to_json(report: RunReport) -> dict[str, Any]:
    """Plain, JSON-ready dict -- the wire shape for a CLI `--report` flag or
    an external observability sink. Additive: new keys may be appended in
    future minor versions, existing keys are not renamed or removed without
    a schema_version bump (mirrors `trace_to_json`'s contract)."""
    return {
        "schema_version": 1,
        "report_kind": "agentlog-run-report",
        "agent_name": report.agent_name,
        "run_id": report.run_id,
        "terminal_status": report.terminal_status,
        "event_count": report.event_count,
        "event_type_counts": report.event_type_counts,
        "steps": {
            "model_step_count": report.model_step_count,
            "tool_call_count": report.tool_call_count,
        },
        "tool_outcomes": {
            "succeeded": report.tool_call_succeeded_count,
            "failed": report.tool_call_failed_count,
            "rejected": report.tool_call_rejected_count,
        },
        "validation": {
            "retryable_failures": report.validation_retry_count,
            "non_retryable_failures": report.validation_non_retryable_failure_count,
        },
        "control": {
            "goal_policy_observed": report.goal_policy_observed,
            "goal_satisfied": report.goal_satisfied,
            "workflow_invariant_violated": report.workflow_invariant_violated,
            "workflow_cycle_detected": report.workflow_cycle_detected,
            "abstained": report.abstained,
        },
        "latency_seconds": {
            "model": list(report.model_latency_seconds),
            "tool_call": list(report.tool_call_latency_seconds),
        },
    }
