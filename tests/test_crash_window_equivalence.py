from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentlog.evals import InvocationObservation, evaluate_crash_window

from tests.model.crash_window_harness import CrashWindowHarness


class CrashWindowEquivalenceTests(unittest.TestCase):
    def evidence(self, kind: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return CrashWindowHarness(Path(directory.name) / "events.db").run_boundary(kind)

    def test_model_and_tool_retry_in_fresh_runtime_with_one_committed_result(self):
        for kind in ("model", "tool"):
            with self.subTest(kind=kind):
                evidence = self.evidence(kind)
                self.assertEqual(evaluate_crash_window(evidence), ())
                self.assertEqual(len(evidence.invocations), 2)
                self.assertEqual(
                    {item.runtime_generation for item in evidence.invocations}, {1, 2}
                )

    def test_boundary_mutants_are_detected(self):
        evidence = self.evidence("model")
        result = next(
            event
            for event in evidence.trace.events
            if event.event_type == evidence.result_event_type
        )
        duplicate = replace(result, event_id="duplicate-result")
        mutants = {
            "new_operation_id": replace(
                evidence,
                invocations=evidence.invocations[:-1]
                + (replace(evidence.invocations[-1], operation_id="new-operation"),),
            ),
            "changed_causation": replace(
                evidence,
                trace=replace(
                    evidence.trace,
                    events=tuple(
                        replace(event, causation_id="wrong")
                        if event.event_type == evidence.result_event_type
                        else event
                        for event in evidence.trace.events
                    ),
                ),
            ),
            "second_result": replace(
                evidence,
                trace=replace(
                    evidence.trace, events=evidence.trace.events + (duplicate,)
                ),
            ),
            "early_checkpoint": replace(
                evidence, checkpoint_after_crash=evidence.request_global_position
            ),
            "skip_retry": replace(evidence, invocations=evidence.invocations[:1]),
            "stale_runtime_result": replace(
                evidence,
                invocations=tuple(
                    replace(invocation, runtime_generation=1)
                    for invocation in evidence.invocations
                ),
            ),
            "collapsed_physical_invocation": replace(
                evidence,
                invocations=(
                    InvocationObservation(
                        "model", evidence.request_event_id, runtime_generation=2
                    ),
                ),
            ),
        }
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertTrue(evaluate_crash_window(mutant), name)


if __name__ == "__main__":
    unittest.main()
