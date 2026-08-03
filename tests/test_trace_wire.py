import unittest
from datetime import datetime, timezone
from uuid import UUID

from aiq import (
    AgentDefinition,
    Event,
    EventEnvelope,
    build_causal_trace,
    trace_to_json,
)


EVENT_TYPES = (
    "UserMessageAdded",
    "ModelCallRequested",
    "ModelCallSucceeded",
    "ToolCallRequested",
    "ToolCallSucceeded",
    "ModelCallRequested",
    "ModelCallSucceeded",
    "AnswerProduced",
    "RunCompleted",
)
GLOBAL_POSITIONS = (10, 13, 14, 17, 21, 22, 26, 30, 31)


def event_id(index: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{index:012d}")


def reference_history() -> tuple[EventEnvelope, ...]:
    correlation_id = "10000000-0000-4000-8000-000000000001"
    envelopes = []
    for version, (event_type, global_position) in enumerate(
        zip(EVENT_TYPES, GLOBAL_POSITIONS)
    ):
        metadata = {"correlation_id": correlation_id}
        if version > 0:
            cause_version = version - 1
            if version == 2:
                cause_version = 0
            metadata["causation_id"] = str(event_id(cause_version + 1))
        if event_type in {
            "ModelCallRequested",
            "ToolCallRequested",
        }:
            metadata["operation_id"] = str(event_id(version + 1))
        elif event_type in {
            "ModelCallSucceeded",
            "ToolCallSucceeded",
        }:
            request_version = version - 1
            metadata["operation_id"] = str(event_id(request_version + 1))
        event = Event(
            event_type,
            {"version": version},
            metadata,
            event_id=event_id(version + 1),
        )
        envelopes.append(
            EventEnvelope(
                stream_id="agent-a:run-1",
                stream_version=version,
                global_position=global_position,
                event=event,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
    return tuple(envelopes)


class TraceWireContractTests(unittest.TestCase):
    def test_versioned_nine_event_domain_graph_contract(self) -> None:
        agent = AgentDefinition(
            "agent-a",
            initial_state=lambda: None,
            terminal_event_types={"RunCompleted"},
        )
        document = trace_to_json(
            build_causal_trace(
                agent_name="agent-a",
                run_id="run-1",
                agent=agent,
                history=tuple(reversed(reference_history())),
            )
        )

        self.assertEqual(
            set(document),
            {
                "schema_version",
                "graph_kind",
                "agent_name",
                "run_id",
                "terminal_status",
                "latest_stream_version",
                "roots",
                "nodes",
                "edges",
                "timeline",
                "dangling_causation",
            },
        )
        self.assertIs(type(document["schema_version"]), int)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            document["graph_kind"],
            "domain-event-history",
        )
        self.assertEqual(document["terminal_status"], "completed")
        self.assertEqual(
            [node["event_type"] for node in document["nodes"]],
            list(EVENT_TYPES),
        )
        self.assertEqual(
            [node["stream_version"] for node in document["nodes"]],
            list(range(9)),
        )
        self.assertEqual(
            [node["global_position"] for node in document["nodes"]],
            list(GLOBAL_POSITIONS),
        )
        # latest_stream_version must track the final event's own stream_version
        # (8, for 9 events versioned 0..8), never the raw event count (9).
        self.assertEqual(document["latest_stream_version"], 8)
        self.assertNotEqual(document["latest_stream_version"], len(EVENT_TYPES))

        # Timeline is a list of objects, not bare event_id strings, and its
        # order follows stream_version -- which is independent of
        # global_position: GLOBAL_POSITIONS is non-contiguous (gaps of 3, 1,
        # 3, 4, 1, 4, 4, 1) and must not leave gaps or reorder the timeline.
        self.assertEqual(
            document["timeline"],
            [
                {
                    "event_id": str(event_id(index + 1)),
                    "stream_version": index,
                    "global_position": GLOBAL_POSITIONS[index],
                }
                for index in range(9)
            ],
        )
        self.assertEqual(
            [entry["stream_version"] for entry in document["timeline"]],
            sorted(entry["stream_version"] for entry in document["timeline"]),
        )

        self.assertEqual(
            document["roots"],
            [str(event_id(1))],
        )
        self.assertIn(
            {
                "source_event_id": str(event_id(1)),
                "target_event_id": str(event_id(3)),
                "kind": "caused",
            },
            document["edges"],
        )
        self.assertNotIn(
            {
                "source_event_id": str(event_id(2)),
                "target_event_id": str(event_id(3)),
                "kind": "caused",
            },
            document["edges"],
        )
        # The pre-finalization edge shape must not survive anywhere.
        for edge in document["edges"]:
            self.assertEqual(
                set(edge),
                {"source_event_id", "target_event_id", "kind"},
            )
            self.assertNotIn("cause_event_id", edge)
            self.assertNotIn("effect_event_id", edge)
            self.assertEqual(edge["kind"], "caused")
            self.assertNotEqual(edge["kind"], "causes")
        self.assertEqual(document["dangling_causation"], [])

    def test_operation_id_is_read_not_derived(self) -> None:
        """trace.py must surface operation_id only if metadata already has it
        -- never fall back to computing str(event_id) on its own."""
        request_without_operation_id = Event(
            "ModelCallRequested",
            {},
            {},  # deliberately no operation_id
            event_id=event_id(1),
        )
        agent = AgentDefinition("agent-a", initial_state=lambda: None)
        document = trace_to_json(
            build_causal_trace(
                agent_name="agent-a",
                run_id="run-1",
                agent=agent,
                history=(
                    EventEnvelope(
                        stream_id="agent-a:run-1",
                        stream_version=0,
                        global_position=1,
                        event=request_without_operation_id,
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    ),
                ),
            )
        )

        self.assertIsNone(document["nodes"][0]["operation_id"])
        # Not silently backfilled with the event's own id, even though that
        # value would trivially be available here.
        self.assertNotEqual(document["nodes"][0]["operation_id"], str(event_id(1)))

    def test_unknown_event_and_dangling_cause_remain_explicit(self) -> None:
        missing = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        unknown = Event(
            "FutureEventType",
            {"new_field": {"nested": True}},
            {
                "causation_id": missing,
                "unknown_metadata": "kept",
            },
            event_id=event_id(99),
        )
        agent = AgentDefinition("agent-a", initial_state=lambda: None)
        document = trace_to_json(
            build_causal_trace(
                agent_name="agent-a",
                run_id="run-1",
                agent=agent,
                history=(
                    EventEnvelope(
                        stream_id="agent-a:run-1",
                        stream_version=0,
                        global_position=50,
                        event=unknown,
                        created_at=datetime(
                            2026,
                            1,
                            1,
                            tzinfo=timezone.utc,
                        ),
                    ),
                ),
            )
        )

        self.assertEqual(
            document["nodes"][0]["event_type"],
            "FutureEventType",
        )
        self.assertEqual(
            document["nodes"][0]["data"]["new_field"]["nested"],
            True,
        )
        self.assertEqual(
            document["nodes"][0]["metadata"]["unknown_metadata"],
            "kept",
        )
        self.assertEqual(
            document["dangling_causation"],
            [
                {
                    "event_id": str(event_id(99)),
                    "missing_causation_id": missing,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
