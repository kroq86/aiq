from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from agentlog.trace import CausalTrace

from .case import EvalCase
from .report import TraceSummary


@dataclass(frozen=True, slots=True)
class RestartPoint:
    boundary: str

    def __post_init__(self) -> None:
        if not self.boundary:
            raise ValueError("restart boundary must not be empty")


class UnsupportedRestartPoint(ValueError):
    """The executor cannot reproduce this declared durable boundary."""


class RestartableTraceExecutor(Protocol):
    async def run_normal(self, case: EvalCase) -> CausalTrace: ...

    async def restart_points(
        self, case: EvalCase, normal_trace: CausalTrace
    ) -> tuple[RestartPoint, ...]: ...

    async def run_restarted(
        self, case: EvalCase, point: RestartPoint
    ) -> CausalTrace: ...


@dataclass(frozen=True, slots=True)
class RestartDifference:
    field: str
    normal: object
    restarted: object


RestartScenarioStatus = Literal["matched", "mismatched", "unsupported"]


@dataclass(frozen=True, slots=True)
class RestartScenarioResult:
    point: RestartPoint
    status: RestartScenarioStatus
    differences: tuple[RestartDifference, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RestartEquivalenceResult:
    case: EvalCase
    normal_trace: CausalTrace
    scenarios: tuple[RestartScenarioResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.scenarios) and all(
            scenario.status == "matched" for scenario in self.scenarios
        )


def compare_restart_traces(
    normal: CausalTrace, restarted: CausalTrace
) -> tuple[RestartDifference, ...]:
    before = TraceSummary.from_trace(normal)
    after = TraceSummary.from_trace(restarted)
    values = (
        ("terminal_status", before.terminal_status, after.terminal_status),
        ("tool_trajectory", before.tool_trajectory, after.tool_trajectory),
        ("model_steps", before.model_steps, after.model_steps),
        ("tool_failures", before.tool_failures, after.tool_failures),
        (
            "committed_observations",
            before.committed_observation_digest,
            after.committed_observation_digest,
        ),
        ("causal_shape", before.causal_shape_digest, after.causal_shape_digest),
        (
            "operation_identity_relations",
            before.operation_relation_digest,
            after.operation_relation_digest,
        ),
    )
    return tuple(
        RestartDifference(field, normal_value, restarted_value)
        for field, normal_value, restarted_value in values
        if normal_value != restarted_value
    )


class RestartEquivalenceRunner:
    def __init__(self, executor: RestartableTraceExecutor) -> None:
        self._executor = executor

    async def run_case(self, case: EvalCase) -> RestartEquivalenceResult:
        normal = await self._executor.run_normal(case)
        points = await self._executor.restart_points(case, normal)
        if len({point.boundary for point in points}) != len(points):
            raise ValueError("restart executor returned duplicate boundaries")
        scenarios: list[RestartScenarioResult] = []
        for point in points:
            try:
                restarted = await self._executor.run_restarted(case, point)
            except UnsupportedRestartPoint as error:
                scenarios.append(
                    RestartScenarioResult(
                        point, "unsupported", reason=str(error) or point.boundary
                    )
                )
                continue
            differences = compare_restart_traces(normal, restarted)
            scenarios.append(
                RestartScenarioResult(
                    point,
                    "mismatched" if differences else "matched",
                    differences,
                )
            )
        return RestartEquivalenceResult(case, normal, tuple(scenarios))


@dataclass(frozen=True, slots=True)
class InvocationObservation:
    kind: Literal["model", "tool"]
    operation_id: str
    runtime_generation: int


@dataclass(frozen=True, slots=True)
class CrashWindowEvidence:
    kind: Literal["model", "tool"]
    trace: CausalTrace
    request_event_id: str
    request_global_position: int
    checkpoint_after_crash: int
    result_event_type: str
    invocations: tuple[InvocationObservation, ...]


def evaluate_crash_window(
    evidence: CrashWindowEvidence,
) -> tuple[RestartDifference, ...]:
    differences: list[RestartDifference] = []
    matching_invocations = tuple(
        invocation
        for invocation in evidence.invocations
        if invocation.kind == evidence.kind
    )
    if len(matching_invocations) < 2:
        differences.append(
            RestartDifference(
                "required_retry", ">=2 physical invocations", len(matching_invocations)
            )
        )
    operation_ids = tuple(
        invocation.operation_id for invocation in matching_invocations
    )
    if any(
        operation_id != evidence.request_event_id for operation_id in operation_ids
    ):
        differences.append(
            RestartDifference(
                "operation_identity",
                evidence.request_event_id,
                operation_ids,
            )
        )
    generations = {invocation.runtime_generation for invocation in matching_invocations}
    if len(generations) < 2:
        differences.append(
            RestartDifference(
                "fresh_runtime_retry", ">=2 runtime generations", tuple(sorted(generations))
            )
        )
    results = tuple(
        event
        for event in evidence.trace.events
        if event.event_type == evidence.result_event_type
        and event.causation_id == evidence.request_event_id
    )
    if len(results) != 1:
        differences.append(
            RestartDifference("committed_result_count", 1, len(results))
        )
    elif (
        results[0].operation_id != evidence.request_event_id
        or results[0].causation_id != evidence.request_event_id
    ):
        differences.append(
            RestartDifference(
                "result_identity",
                (evidence.request_event_id, evidence.request_event_id),
                (results[0].operation_id, results[0].causation_id),
            )
        )
    if evidence.checkpoint_after_crash >= evidence.request_global_position:
        differences.append(
            RestartDifference(
                "checkpoint_before_result_commit",
                f"< {evidence.request_global_position}",
                evidence.checkpoint_after_crash,
            )
        )
    return tuple(differences)
