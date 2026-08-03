from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiq.trace import CausalTrace, TraceEvent

from .assertions import AssertionFailure
from .runner import EvalRunResult


def format_failure(failure: AssertionFailure) -> str:
    return (
        f"{failure.assertion}: expected={failure.expected!r}, "
        f"actual={failure.actual!r}: {failure.message}"
    )


def _event_kind(event_type: str) -> str:
    for kind in (
        "ModelCallRequested",
        "ModelCallSucceeded",
        "ModelCallFailed",
        "ToolCallRequested",
        "ToolCallSucceeded",
        "ToolCallFailed",
        "RunCompleted",
        "RunFailed",
    ):
        if event_type.endswith(kind):
            return kind
    return event_type


def _tool_name(event: TraceEvent) -> str:
    call = event.data.get("call") or event.data.get("tool_call")
    if isinstance(call, Mapping):
        name = call.get("name")
        if isinstance(name, str):
            return name
    name = event.data.get("name")
    return str(name) if name is not None else ""


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TraceSummary:
    terminal_status: str
    tool_trajectory: tuple[str, ...]
    model_steps: int
    tool_failures: int
    causal_shape_digest: str
    operation_relation_digest: str
    committed_observation_digest: str

    @classmethod
    def from_trace(cls, trace: CausalTrace) -> TraceSummary:
        positions = {event.event_id: index for index, event in enumerate(trace.events)}
        causal_shape = tuple(
            (
                _event_kind(event.event_type),
                positions.get(event.causation_id) if event.causation_id else None,
            )
            for event in trace.events
        )
        operation_groups: dict[str, list[int]] = {}
        for index, event in enumerate(trace.events):
            if event.operation_id is not None:
                operation_groups.setdefault(event.operation_id, []).append(index)
        operation_relations = tuple(
            sorted(tuple(group) for group in operation_groups.values())
        )
        committed_observations = tuple(
            (_event_kind(event.event_type), event.data) for event in trace.events
        )
        return cls(
            terminal_status=trace.terminal_status,
            tool_trajectory=tuple(
                _tool_name(event)
                for event in trace.events
                if _event_kind(event.event_type) == "ToolCallRequested"
            ),
            model_steps=sum(
                _event_kind(event.event_type) == "ModelCallRequested"
                for event in trace.events
            ),
            tool_failures=sum(
                _event_kind(event.event_type) == "ToolCallFailed"
                for event in trace.events
            ),
            causal_shape_digest=_digest(causal_shape),
            operation_relation_digest=_digest(operation_relations),
            committed_observation_digest=_digest(committed_observations),
        )

    @classmethod
    def from_dict(cls, value: object) -> TraceSummary:
        if not isinstance(value, Mapping):
            raise TypeError("trace_summary must be an object")
        trajectory = value.get("tool_trajectory", ())
        if not isinstance(trajectory, list) or not all(
            isinstance(item, str) for item in trajectory
        ):
            raise TypeError("trace_summary.tool_trajectory must be an array of strings")
        return cls(
            terminal_status=str(value["terminal_status"]),
            tool_trajectory=tuple(trajectory),
            model_steps=int(value["model_steps"]),
            tool_failures=int(value["tool_failures"]),
            causal_shape_digest=str(value["causal_shape_digest"]),
            operation_relation_digest=str(value["operation_relation_digest"]),
            committed_observation_digest=str(value["committed_observation_digest"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal_status": self.terminal_status,
            "tool_trajectory": list(self.tool_trajectory),
            "model_steps": self.model_steps,
            "tool_failures": self.tool_failures,
            "causal_shape_digest": self.causal_shape_digest,
            "operation_relation_digest": self.operation_relation_digest,
            "committed_observation_digest": self.committed_observation_digest,
        }


@dataclass(frozen=True, slots=True)
class EvalCaseReport:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    trace_summary: TraceSummary

    def __getitem__(self, key: str) -> object:
        """Compatibility for the initial dict-shaped report API."""
        return self.to_dict()[key]

    @classmethod
    def from_dict(cls, value: object) -> EvalCaseReport:
        if not isinstance(value, Mapping):
            raise TypeError("eval report case must be an object")
        failures = value.get("failures", ())
        if not isinstance(failures, list) or not all(
            isinstance(item, str) for item in failures
        ):
            raise TypeError("eval report failures must be an array of strings")
        passed = value.get("passed")
        if not isinstance(passed, bool):
            raise TypeError("eval report passed must be a boolean")
        return cls(
            case_id=str(value["id"]),
            passed=passed,
            failures=tuple(failures),
            trace_summary=TraceSummary.from_dict(value["trace_summary"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "passed": self.passed,
            "failures": list(self.failures),
            "trace_summary": self.trace_summary.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    dataset: str
    total: int
    passed: int
    failed: int
    cases: tuple[EvalCaseReport, ...]

    def __post_init__(self) -> None:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("eval report contains duplicate case ids")
        if self.total != len(self.cases):
            raise ValueError("eval report total does not match cases")
        if self.passed + self.failed != self.total:
            raise ValueError("eval report counts do not add up to total")

    @classmethod
    def from_result(cls, dataset: str, result: EvalRunResult) -> EvalReport:
        cases = tuple(
            EvalCaseReport(
                case_id=case_result.case.case_id or f"case-{index}",
                passed=case_result.passed,
                failures=tuple(
                    format_failure(failure) for failure in case_result.failures
                ),
                trace_summary=TraceSummary.from_trace(case_result.trace),
            )
            for index, case_result in enumerate(result.cases, start=1)
        )
        return cls(
            dataset=dataset,
            total=len(cases),
            passed=sum(case.passed for case in cases),
            failed=sum(not case.passed for case in cases),
            cases=cases,
        )

    @classmethod
    def from_dict(cls, value: object) -> EvalReport:
        if not isinstance(value, Mapping):
            raise TypeError("eval report must be an object")
        raw_cases = value.get("cases")
        if not isinstance(raw_cases, list):
            raise TypeError("eval report cases must be an array")
        return cls(
            dataset=str(value["dataset"]),
            total=int(value["total"]),
            passed=int(value["passed"]),
            failed=int(value["failed"]),
            cases=tuple(EvalCaseReport.from_dict(case) for case in raw_cases),
        )

    @classmethod
    def load(cls, path: str | Path) -> EvalReport:
        with Path(path).open(encoding="utf-8") as source:
            return cls.from_dict(json.load(source))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "dataset": self.dataset,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")
