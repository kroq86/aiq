from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .report import EvalCaseReport, EvalReport

ComparisonStatus = Literal[
    "added", "missing", "regressed", "improved", "changed", "unchanged"
]


@dataclass(frozen=True, slots=True)
class EvalDifference:
    field: str
    baseline: object
    candidate: object

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "baseline": self.baseline,
            "candidate": self.candidate,
        }


@dataclass(frozen=True, slots=True)
class EvalCaseComparison:
    case_id: str
    status: ComparisonStatus
    differences: tuple[EvalDifference, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "status": self.status,
            "differences": [difference.to_dict() for difference in self.differences],
        }


@dataclass(frozen=True, slots=True)
class EvalComparison:
    baseline: str
    candidate: str
    cases: tuple[EvalCaseComparison, ...]

    @property
    def regression_count(self) -> int:
        return sum(case.status in {"regressed", "missing"} for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "regressions": self.regression_count,
            "cases": [case.to_dict() for case in self.cases],
        }


def _differences(
    baseline: EvalCaseReport, candidate: EvalCaseReport
) -> tuple[EvalDifference, ...]:
    baseline_summary = baseline.trace_summary
    candidate_summary = candidate.trace_summary
    values = (
        ("passed", baseline.passed, candidate.passed),
        (
            "terminal_status",
            baseline_summary.terminal_status,
            candidate_summary.terminal_status,
        ),
        (
            "tool_trajectory",
            baseline_summary.tool_trajectory,
            candidate_summary.tool_trajectory,
        ),
        ("model_steps", baseline_summary.model_steps, candidate_summary.model_steps),
        (
            "tool_failures",
            baseline_summary.tool_failures,
            candidate_summary.tool_failures,
        ),
        (
            "causal_shape",
            baseline_summary.causal_shape_digest,
            candidate_summary.causal_shape_digest,
        ),
        (
            "operation_identity_relations",
            baseline_summary.operation_relation_digest,
            candidate_summary.operation_relation_digest,
        ),
        (
            "committed_observations",
            baseline_summary.committed_observation_digest,
            candidate_summary.committed_observation_digest,
        ),
    )
    return tuple(
        EvalDifference(field, before, after)
        for field, before, after in values
        if before != after
    )


def compare_reports(baseline: EvalReport, candidate: EvalReport) -> EvalComparison:
    baseline_by_id = {case.case_id: case for case in baseline.cases}
    candidate_by_id = {case.case_id: case for case in candidate.cases}
    ordered_ids = tuple(baseline_by_id) + tuple(
        case_id for case_id in candidate_by_id if case_id not in baseline_by_id
    )
    comparisons: list[EvalCaseComparison] = []
    for case_id in ordered_ids:
        before = baseline_by_id.get(case_id)
        after = candidate_by_id.get(case_id)
        if before is None:
            comparisons.append(EvalCaseComparison(case_id, "added", ()))
            continue
        if after is None:
            comparisons.append(EvalCaseComparison(case_id, "missing", ()))
            continue
        differences = _differences(before, after)
        if before.passed and not after.passed:
            status: ComparisonStatus = "regressed"
        elif not before.passed and after.passed:
            status = "improved"
        elif differences:
            status = "changed"
        else:
            status = "unchanged"
        comparisons.append(EvalCaseComparison(case_id, status, differences))
    return EvalComparison(baseline.dataset, candidate.dataset, tuple(comparisons))
