from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _expect_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return value


def _optional_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class EvalAssertions:
    no_tool_failure: bool = False
    stable_operation_ids: bool = False

    @classmethod
    def from_dict(cls, value: object) -> EvalAssertions:
        data = _expect_mapping(value, field_name="assertions")
        unknown = set(data).difference({"no_tool_failure", "stable_operation_ids"})
        if unknown:
            raise ValueError(f"unknown eval assertions: {', '.join(sorted(unknown))}")
        return cls(
            no_tool_failure=_optional_bool(
                data.get("no_tool_failure", False), field_name="assertions.no_tool_failure"
            ),
            stable_operation_ids=_optional_bool(
                data.get("stable_operation_ids", False),
                field_name="assertions.stable_operation_ids",
            ),
        )


@dataclass(frozen=True, slots=True)
class EvalCase:
    input: str
    expected_tools: tuple[str, ...] = ()
    expected_terminal: str | None = None
    max_model_steps: int | None = None
    assertions: EvalAssertions = field(default_factory=EvalAssertions)
    case_id: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> EvalCase:
        data = _expect_mapping(value, field_name="eval case")
        allowed = {
            "id",
            "input",
            "expected_tools",
            "expected_terminal",
            "max_model_steps",
            "assertions",
        }
        unknown = set(data).difference(allowed)
        if unknown:
            raise ValueError(f"unknown eval case fields: {', '.join(sorted(unknown))}")

        prompt = data.get("input")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("eval case input must be a non-empty string")

        raw_tools = data.get("expected_tools", ())
        if isinstance(raw_tools, (str, bytes)) or not isinstance(raw_tools, Sequence):
            raise TypeError("expected_tools must be an array of strings")
        if any(not isinstance(tool, str) or not tool for tool in raw_tools):
            raise ValueError("expected_tools must contain non-empty strings")

        terminal = data.get("expected_terminal")
        if terminal is not None and (not isinstance(terminal, str) or not terminal):
            raise ValueError("expected_terminal must be a non-empty string or null")

        max_steps = data.get("max_model_steps")
        if max_steps is not None and (
            isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0
        ):
            raise ValueError("max_model_steps must be a positive integer or null")

        case_id = data.get("id")
        if case_id is not None and (not isinstance(case_id, str) or not case_id):
            raise ValueError("id must be a non-empty string or null")

        return cls(
            input=prompt,
            expected_tools=tuple(raw_tools),
            expected_terminal=terminal,
            max_model_steps=max_steps,
            assertions=EvalAssertions.from_dict(data.get("assertions", {})),
            case_id=case_id,
        )


@dataclass(frozen=True, slots=True)
class EvalDataset:
    cases: tuple[EvalCase, ...]
    name: str | None = None
    executor: str | None = None

    def __post_init__(self) -> None:
        if not self.cases:
            raise ValueError("eval dataset must contain at least one case")

    @classmethod
    def from_data(cls, value: object) -> EvalDataset:
        raw_cases: object
        if isinstance(value, Mapping):
            unknown = set(value).difference({"name", "executor", "cases"})
            if unknown:
                raise ValueError(f"unknown eval dataset fields: {', '.join(sorted(unknown))}")
            raw_cases = value.get("cases")
            name = value.get("name")
            executor = value.get("executor")
        else:
            raw_cases = value
            name = None
            executor = None
        if isinstance(raw_cases, (str, bytes)) or not isinstance(raw_cases, Sequence):
            raise TypeError("eval dataset must be an array or an object containing cases")
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("eval dataset name must be a non-empty string or null")
        if executor is not None and (not isinstance(executor, str) or not executor):
            raise ValueError("eval dataset executor must be a non-empty string or null")
        return cls(
            tuple(EvalCase.from_dict(item) for item in raw_cases),
            name=name,
            executor=executor,
        )

    @classmethod
    def load(cls, path: str | Path) -> EvalDataset:
        with Path(path).open(encoding="utf-8") as source:
            data: Any = json.load(source)
        return cls.from_data(data)
