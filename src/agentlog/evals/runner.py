from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from agentlog.trace import CausalTrace

from .assertions import AssertionFailure, evaluate_trace
from .case import EvalCase, EvalDataset


class TraceExecutor(Protocol):
    def __call__(self, case: EvalCase) -> Awaitable[CausalTrace]: ...


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case: EvalCase
    trace: CausalTrace
    failures: tuple[AssertionFailure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    cases: tuple[EvalCaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    @property
    def passed_count(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def failed_count(self) -> int:
        return len(self.cases) - self.passed_count


class EvalRunner:
    """Evaluate traces produced by an injected durable-run adapter.

    Execution stays outside the eval core: the adapter owns provider/tool I/O,
    persistence, restart policy, and trace export. Cases run sequentially so
    the runner introduces no hidden concurrency or resource sharing.
    """

    def __init__(self, executor: TraceExecutor) -> None:
        self._executor = executor

    async def run_case(self, case: EvalCase) -> EvalCaseResult:
        trace = await self._executor(case)
        return EvalCaseResult(case, trace, evaluate_trace(case, trace))

    async def run(self, dataset: EvalDataset) -> EvalRunResult:
        results = tuple([await self.run_case(case) for case in dataset.cases])
        return EvalRunResult(results)
