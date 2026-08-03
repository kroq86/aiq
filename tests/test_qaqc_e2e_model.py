from __future__ import annotations

import asyncio
import tempfile
import unittest

from aiq import ArtifactRef
from aiq.evals import compare_restart_traces, evaluate_crash_window
from formal.model.spec import ReferenceState, assert_invariants
from tests.model.normalization import normalize_history
from tests.model.qaqc_e2e_harness import (
    DATASET,
    DATASET_DIGEST,
    ETAG,
    RULES_VERSION,
    execute,
    execute_save_report_crash,
)


class QAQCEndToEndModelTests(unittest.TestCase):
    def test_mcp_datalake_artifact_trace_matches_after_every_dispatch_restart(self):
        normal = asyncio.run(execute(restart_after_every_dispatch=False))
        restarted = asyncio.run(execute(restart_after_every_dispatch=True))
        normal_trace, normal_history, normal_gateway, normal_artifacts = normal
        restart_trace, restart_history, restart_gateway, restart_artifacts = restarted

        self.assertEqual(compare_restart_traces(normal_trace, restart_trace), ())
        self.assertEqual(normal_trace.terminal_status, "completed")
        expected_tools = (
            "list_rules",
            "read_dataset_metadata",
            "run_qaqc",
            "save_report",
        )
        self.assertEqual(
            tuple(item[0] for item in normal_gateway.invocations), expected_tools
        )
        self.assertEqual(
            tuple(item[0] for item in restart_gateway.invocations), expected_tools
        )
        self.assertEqual(
            len({operation for _, operation, _ in restart_gateway.invocations}), 4
        )

        qaqc_arguments = restart_gateway.invocations[2][2]
        self.assertEqual(qaqc_arguments["pinned_path"], f"{DATASET}@{ETAG}")
        self.assertEqual(qaqc_arguments["dataset_digest"], DATASET_DIGEST)
        self.assertEqual(qaqc_arguments["rules_version"], RULES_VERSION)

        report_result = next(
            event.event.data["result"]
            for event in restart_history
            if event.event.event_type == "ToolCallSucceeded"
            and event.event.data["name"] == "save_report"
        )
        report_ref = restart_artifacts.refs[("qaqc-report.json", "report-v1")]
        self.assertEqual(report_result["digest"], report_ref.digest)
        self.assertEqual(report_result["version"], "report-v1")
        self.assertEqual(normal_artifacts.refs.keys(), restart_artifacts.refs.keys())

        # The product-independent reference invariant oracle accepts the full
        # four-tool history. This is safety evidence, not transition refinement:
        # the canonical reference planner currently chooses only one tool.
        for history in (normal_history, restart_history):
            normalized = normalize_history(history)
            state = ReferenceState(normalized, len(normalized), len(normalized))
            assert_invariants(ReferenceState((), 0, 0), state)

    def test_mcp_policy_denial_is_durable_rejection_without_report(self):
        normal = asyncio.run(
            execute(
                restart_after_every_dispatch=False,
                deny_dataset_metadata=True,
            )
        )
        restarted = asyncio.run(
            execute(
                restart_after_every_dispatch=True,
                deny_dataset_metadata=True,
            )
        )
        normal_trace, normal_history, normal_gateway, normal_artifacts = normal
        restart_trace, restart_history, restart_gateway, restart_artifacts = restarted

        self.assertEqual(compare_restart_traces(normal_trace, restart_trace), ())
        self.assertEqual(normal_trace.terminal_status, "failed")
        self.assertEqual(
            tuple(item[0] for item in normal_gateway.invocations),
            ("list_rules", "read_dataset_metadata"),
        )
        self.assertEqual(
            tuple(item[0] for item in restart_gateway.invocations),
            ("list_rules", "read_dataset_metadata"),
        )
        event_types = tuple(item.event.event_type for item in restart_history)
        self.assertIn("ToolCallRejected", event_types)
        self.assertEqual(event_types[-1], "RunFailed")
        self.assertNotIn(
            "run_qaqc", tuple(item[0] for item in restart_gateway.invocations)
        )
        self.assertNotIn(
            "save_report", tuple(item[0] for item in restart_gateway.invocations)
        )
        self.assertFalse(normal_artifacts.refs)
        self.assertFalse(restart_artifacts.refs)

        rejection = next(
            item.event
            for item in restart_history
            if item.event.event_type == "ToolCallRejected"
        )
        self.assertEqual(
            rejection.metadata["operation_id"], rejection.metadata["causation_id"]
        )
        for history in (normal_history, restart_history):
            normalized = normalize_history(history)
            state = ReferenceState(normalized, len(normalized), len(normalized))
            assert_invariants(ReferenceState((), 0, 0), state)

    def test_save_report_retries_after_invocation_before_result_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence, history, artifacts, operational_log = asyncio.run(
                execute_save_report_crash(f"{directory}/events.db")
            )
            committed_report = next(
                item.event.data["result"]
                for item in history
                if item.event.event_type == "ToolCallSucceeded"
                and item.event.data["name"] == "save_report"
            )
            report_ref = ArtifactRef.from_data(committed_report)
            registered_ref = asyncio.run(artifacts.get(report_ref))

        self.assertEqual(evaluate_crash_window(evidence), ())
        save_calls = tuple(item for item in operational_log if item[0] == "save_report")
        self.assertEqual(len(save_calls), 2)
        self.assertEqual({item[2] for item in save_calls}, {1, 2})
        self.assertEqual({item[1] for item in save_calls}, {evidence.request_event_id})
        committed = tuple(
            item.event
            for item in history
            if item.event.event_type == "ToolCallSucceeded"
            and item.event.metadata.get("causation_id") == evidence.request_event_id
        )
        self.assertEqual(len(committed), 1)
        self.assertEqual(
            committed[0].metadata["operation_id"], evidence.request_event_id
        )
        self.assertEqual(
            tuple(item.event.event_type for item in history).count("RunCompleted"), 1
        )
        self.assertEqual(registered_ref, report_ref)
        self.assertEqual(committed[0].data["result"]["digest"], report_ref.digest)


if __name__ == "__main__":
    unittest.main()
