from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from agentlog import (
    ArtifactDigestMismatchError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactVersionConflictError,
    Agent,
    DurableDispatcher,
    DurableEffectDispatcher,
    DurableModelLoop,
    Event,
    InMemoryArtifactStore,
    InMemoryEventStore,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolRegistry,
    run_stream_id,
)


def run(coro):
    return asyncio.run(coro)


class ArtifactTests(unittest.TestCase):
    def test_default_version_is_content_addressed_and_put_is_idempotent(self):
        store = InMemoryArtifactStore()
        first = run(store.put("policy.pdf", b"policy", media_type="application/pdf"))
        second = run(store.put("policy.pdf", b"policy", media_type="application/pdf"))
        self.assertEqual(first, second)
        self.assertEqual(run(store.get(first)), b"policy")
        self.assertEqual(first.version, first.digest.removeprefix("sha256:"))

    def test_explicit_version_is_immutable(self):
        store = InMemoryArtifactStore()
        run(
            store.put(
                "policy.pdf", b"v1", media_type="application/pdf", version="approved"
            )
        )
        with self.assertRaises(ArtifactVersionConflictError):
            run(
                store.put(
                    "policy.pdf",
                    b"changed",
                    media_type="application/pdf",
                    version="approved",
                )
            )

    def test_missing_and_digest_mismatch_are_explicit(self):
        store = InMemoryArtifactStore()
        ref = run(store.put("policy.pdf", b"policy", media_type="application/pdf"))
        missing = ArtifactRef("other.pdf", ref.version, ref.media_type, ref.digest)
        with self.assertRaises(ArtifactNotFoundError):
            run(store.get(missing))
        changed_media_type = ArtifactRef(ref.name, ref.version, "text/plain", ref.digest)
        with self.assertRaises(ArtifactDigestMismatchError):
            run(store.get(changed_media_type))

    def test_model_request_pins_and_round_trips_exact_refs(self):
        store = InMemoryArtifactStore()
        ref = run(store.put("policy.pdf", b"policy", media_type="application/pdf"))
        request = ModelRequest((ModelMessage("user", "read policy"),), artifacts=(ref,))
        restored = ModelRequest.from_data(request.to_data())
        self.assertEqual(restored, request)
        self.assertEqual(restored.artifacts, (ref,))

    def test_missing_pinned_version_fails_before_provider_invocation(self):
        @dataclass(frozen=True)
        class State:
            pass

        agent = Agent(name="artifact-agent", version="1", initial_state=State)

        @agent.event
        @dataclass(frozen=True)
        class Started:
            pass

        missing = ArtifactRef(
            "policy.pdf", "missing", "application/pdf", f"sha256:{'0' * 64}"
        )
        loop = DurableModelLoop(
            start_on=Started,
            build_request=lambda state, event, definitions: ModelRequest(
                (ModelMessage("user", "policy"),), artifacts=(missing,)
            ),
            tool_definitions=(),
            provider="model",
            tools="tools",
            artifacts="artifacts",
        )
        loop.install(agent)

        class Provider:
            calls = 0

            async def complete(self, request, *, operation_id):
                self.calls += 1
                return ModelResponse(ModelMessage("assistant", "should not run"))

        provider = Provider()
        store = InMemoryEventStore()
        runtime = agent.build_runtime(
            context={
                "model": provider,
                "tools": ToolRegistry(),
                "artifacts": InMemoryArtifactStore(),
            }
        )
        stream_id = run_stream_id("artifact-agent", "missing")
        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated",
                        {"agent": "artifact-agent", "definition_version": "1"},
                    ),
                    Event("Started", {}),
                ),
            )
        )
        reactions = DurableDispatcher(
            agent=runtime.agent, store=store, subscription_name="artifact:reactions"
        )
        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="artifact:effects",
        )
        for _ in range(12):
            if not (run(reactions.run_once()) | run(effects.run_once())):
                break
        history = run(store.load(stream_id))
        event_types = [item.event.event_type for item in history]
        self.assertEqual(provider.calls, 0)
        self.assertIn(loop.events.ArtifactResolutionFailed.__name__, event_types)
        self.assertEqual(event_types[-1], loop.events.RunFailed.__name__)


if __name__ == "__main__":
    unittest.main()
