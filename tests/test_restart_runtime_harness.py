from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from aiq.evals import EvalCase, RestartEquivalenceRunner, compare_restart_traces

from tests.model.restart_eval_harness import BOUNDARIES, ModelLoopRestartExecutor


class RestartRuntimeHarnessTests(unittest.TestCase):
    def test_all_declared_persisted_boundaries_match_normal_execution(self):
        case = EvalCase.from_dict({"id": "weather", "input": "weather"})
        result = asyncio.run(
            RestartEquivalenceRunner(ModelLoopRestartExecutor()).run_case(case)
        )
        self.assertTrue(result.passed)
        self.assertEqual(len(result.scenarios), len(BOUNDARIES))
        self.assertTrue(
            all(scenario.status == "matched" for scenario in result.scenarios)
        )

    def test_normalizer_does_not_hide_semantic_mutants(self):
        executor = ModelLoopRestartExecutor()
        case = EvalCase.from_dict({"input": "weather"})
        normal = asyncio.run(executor.run_normal(case))

        result_index = next(
            index
            for index, event in enumerate(normal.events)
            if event.event_type == "ToolCallSucceeded"
        )
        result = normal.events[result_index]
        request_index = next(
            index
            for index, event in enumerate(normal.events)
            if event.event_type == "ToolCallRequested"
        )
        request = normal.events[request_index]

        mutants = {
            "new_operation_id": replace(result, operation_id="new-operation"),
            "changed_causation": replace(result, causation_id=normal.events[0].event_id),
            "changed_observation": replace(result, data={"result": {"temperature": 99}}),
        }
        for name, changed in mutants.items():
            with self.subTest(mutant=name):
                events = list(normal.events)
                events[result_index] = changed
                mutant_trace = replace(normal, events=tuple(events))
                differences = compare_restart_traces(normal, mutant_trace)
                self.assertTrue(differences, name)

        duplicated = replace(
            result,
            event_id="duplicate-result",
            stream_version=result.stream_version + 1,
            global_position=result.global_position + 1,
            causation_id=request.event_id,
        )
        events = normal.events[: result_index + 1] + (duplicated,) + normal.events[result_index + 1 :]
        duplicate_trace = replace(normal, events=events)
        self.assertTrue(compare_restart_traces(normal, duplicate_trace))


if __name__ == "__main__":
    unittest.main()
