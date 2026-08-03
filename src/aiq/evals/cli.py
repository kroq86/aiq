from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .case import EvalDataset
from .compare import EvalComparison, compare_reports
from .report import EvalReport
from .runner import EvalRunner, TraceExecutor


def _load_executor(reference: str) -> TraceExecutor:
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("executor must use module:callable syntax")
    module = importlib.import_module(module_name)
    executor = getattr(module, attribute_name, None)
    if executor is None or not callable(executor):
        raise TypeError(f"executor is not callable: {reference}")
    if not inspect.iscoroutinefunction(executor):
        raise TypeError(f"executor must be async: {reference}")
    return executor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiq")
    commands = parser.add_subparsers(dest="command", required=True)
    eval_parser = commands.add_parser("eval", help="evaluate durable agent traces")
    eval_commands = eval_parser.add_subparsers(dest="eval_command", required=True)
    run = eval_commands.add_parser("run", help="run an eval dataset")
    run.add_argument("dataset", type=Path)
    run.add_argument("--executor", help="override dataset executor (module:callable)")
    run.add_argument("--json-report", type=Path, help="write a JSON report for CI")
    compare = eval_commands.add_parser(
        "compare", help="compare baseline and candidate JSON reports"
    )
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--json-report", type=Path, help="write comparison JSON")
    return parser


def _print_report(report: EvalReport) -> None:
    print(f"dataset: {report.dataset}")
    print(f"cases total: {report.total}")
    print(f"passed: {report.passed}")
    print(f"failed: {report.failed}")
    for case in report.cases:
        if case.passed:
            continue
        print(f"case {case.case_id}: failed")
        for failure in case.failures:
            print(f"  - {failure}")


def _print_comparison(comparison: EvalComparison) -> None:
    print(f"baseline: {comparison.baseline}")
    print(f"candidate: {comparison.candidate}")
    print(f"regressions: {comparison.regression_count}")
    for case in comparison.cases:
        if case.status != "unchanged":
            print(f"case {case.case_id}: {case.status}")


async def _run(dataset_path: Path, executor_override: str | None) -> EvalReport:
    dataset = EvalDataset.load(dataset_path)
    executor_reference = executor_override or dataset.executor
    if executor_reference is None:
        raise ValueError("dataset must define executor or --executor must be provided")
    executor = _load_executor(executor_reference)
    result = await EvalRunner(executor).run(dataset)
    return EvalReport.from_result(dataset.name or dataset_path.stem, result)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.eval_command == "run":
            report = asyncio.run(_run(args.dataset, args.executor))
            _print_report(report)
            if args.json_report is not None:
                report.write_json(args.json_report)
            return 0 if report.failed == 0 else 1
        comparison = compare_reports(
            EvalReport.load(args.baseline), EvalReport.load(args.candidate)
        )
        _print_comparison(comparison)
        if args.json_report is not None:
            args.json_report.write_text(
                json.dumps(comparison.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return 1 if comparison.regression_count else 0
    except (ImportError, AttributeError, OSError, TypeError, ValueError) as error:
        print(f"aiq eval: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
