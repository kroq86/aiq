"""Domain-policy seam for validating tool calls and their results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from .core import Event, JsonValue
from .models import ToolCall


def _frozen_mapping(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    frozen = Event("ValidationEvidenceValidated", {"value": value}).data["value"]
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded by Event
        raise TypeError("validation evidence must be an object")
    return frozen


@dataclass(frozen=True, slots=True)
class ToolValidationContext:
    policy_name: str
    model_step: int
    tool_calls_used: int
    operation_id: str
    workflow_state: JsonValue = None


@dataclass(frozen=True, slots=True)
class ValidationAccepted:
    evidence: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))


@dataclass(frozen=True, slots=True)
class ValidationRejected:
    reason: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("validation rejection requires a reason")


@dataclass(frozen=True, slots=True)
class ValidationAmbiguous:
    candidates: tuple[JsonValue, ...]
    reason: str = "ambiguous tool decision"
    retryable: bool = True

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("ambiguous validation requires a reason")
        frozen = Event(
            "ValidationCandidatesValidated", {"candidates": self.candidates}
        ).data["candidates"]
        if not isinstance(frozen, tuple):  # pragma: no cover - guarded by Event
            raise TypeError("validation candidates must be an array")
        object.__setattr__(self, "candidates", frozen)


@dataclass(frozen=True, slots=True)
class PostconditionFailed:
    reason: str

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("postcondition failure requires a reason")


ValidationStatus: TypeAlias = Literal[
    "accept", "reject", "retry", "replan", "abstain", "fail"
]


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    status: ValidationStatus
    code: str | None = None
    message: str | None = None
    evidence: JsonValue = None
    normalized_value: JsonValue = None

    def __post_init__(self) -> None:
        if self.status not in {
            "accept",
            "reject",
            "retry",
            "replan",
            "abstain",
            "fail",
        }:
            raise ValueError(f"unsupported validation status: {self.status!r}")
        if self.status != "accept" and not (self.code or self.message):
            raise ValueError("non-accept validation decisions require code or message")
        frozen = Event(
            "ValidationDecisionValidated",
            {
                "evidence": self.evidence,
                "normalized_value": self.normalized_value,
            },
        ).data
        object.__setattr__(self, "evidence", frozen["evidence"])
        object.__setattr__(self, "normalized_value", frozen["normalized_value"])


RequestValidation: TypeAlias = (
    ValidationAccepted | ValidationRejected | ValidationAmbiguous | ValidationDecision
)
ResultValidation: TypeAlias = (
    ValidationAccepted | PostconditionFailed | ValidationDecision
)


class ToolPolicy(Protocol):
    async def validate_request(
        self, call: ToolCall, context: ToolValidationContext
    ) -> RequestValidation: ...

    async def validate_result(
        self,
        call: ToolCall,
        result: JsonValue,
        evidence: Mapping[str, JsonValue],
        context: ToolValidationContext,
    ) -> ResultValidation: ...


class ExecutionPolicy(Protocol):
    async def validate_input(
        self, call: ToolCall, context: ToolValidationContext
    ) -> ValidationDecision: ...

    async def validate_transition(
        self, call: ToolCall, context: ToolValidationContext
    ) -> ValidationDecision: ...

    async def capture_pre_state(
        self, call: ToolCall, context: ToolValidationContext
    ) -> JsonValue: ...

    async def validate_output(
        self,
        call: ToolCall,
        result: JsonValue,
        evidence: Mapping[str, JsonValue],
        context: ToolValidationContext,
    ) -> ValidationDecision: ...
