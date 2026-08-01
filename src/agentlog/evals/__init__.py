"""Trace-based evaluation primitives for Agentlog runs."""

from .assertions import AssertionFailure, evaluate_trace
from .case import EvalAssertions, EvalCase, EvalDataset
from .compare import (
    EvalCaseComparison,
    EvalComparison,
    EvalDifference,
    compare_reports,
)
from .runner import EvalCaseResult, EvalRunResult, EvalRunner, TraceExecutor
from .report import EvalCaseReport, EvalReport, TraceSummary, format_failure
from .restart import (
    CrashWindowEvidence,
    InvocationObservation,
    RestartDifference,
    RestartEquivalenceResult,
    RestartEquivalenceRunner,
    RestartPoint,
    RestartScenarioResult,
    RestartableTraceExecutor,
    UnsupportedRestartPoint,
    evaluate_crash_window,
    compare_restart_traces,
)

__all__ = [
    "AssertionFailure",
    "EvalAssertions",
    "EvalCase",
    "EvalCaseComparison",
    "EvalCaseReport",
    "EvalCaseResult",
    "EvalDataset",
    "EvalDifference",
    "EvalComparison",
    "EvalRunResult",
    "EvalRunner",
    "EvalReport",
    "CrashWindowEvidence",
    "InvocationObservation",
    "RestartDifference",
    "RestartEquivalenceResult",
    "RestartEquivalenceRunner",
    "RestartPoint",
    "RestartScenarioResult",
    "RestartableTraceExecutor",
    "TraceExecutor",
    "TraceSummary",
    "UnsupportedRestartPoint",
    "compare_reports",
    "compare_restart_traces",
    "evaluate_trace",
    "evaluate_crash_window",
    "format_failure",
]
