"""Manual integration check (needs a running local Ollama -- not part of
`tests/`, which must run with zero external dependencies): crash after the
real Ollama call succeeds, but before the commit persists
ModelCallSucceeded.

Simulated via a store wrapper that raises on the first
commit_subscription_batch call for this stream carrying real output
events (as if the process died right there) -- the Ollama HTTP call
itself is 100% real, nothing about the LLM interaction is faked.

Run:

    PYTHONPATH=src:. python examples/ollama_chat_agent/verify_crash_before_commit.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx

from agentlog import DurableDispatcher, DurableEffectDispatcher, Event, SQLiteEventStore
from examples.ollama_chat_agent.main import OllamaContext, define_agent

ollama_call_count = 0


async def _count_request(request: httpx.Request) -> None:
    global ollama_call_count
    if request.url.path == "/api/chat":
        ollama_call_count += 1
        print(f"  >>> real Ollama HTTP call #{ollama_call_count}")


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=60.0, event_hooks={"request": [_count_request]})


class CrashOnceStore:
    """Wraps a real SQLiteEventStore. The first commit_subscription_batch
    call carrying a non-empty events batch raises, simulating the process
    dying after the external call returned but before the write landed."""

    def __init__(self, inner):
        self._inner = inner
        self._crashed = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def commit_subscription_batch(self, **kwargs):
        if not self._crashed and kwargs.get("events"):
            self._crashed = True
            raise RuntimeError("SIMULATED CRASH: process died before commit")
        return await self._inner.commit_subscription_batch(**kwargs)


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "crash-scenario.db"
        stream_id = "ollama-chat:crash-run"

        agent = define_agent()
        runtime = agent.build_runtime(
            context=OllamaContext(model="llama3.2:1b", client=make_client())
        )

        store = await SQLiteEventStore.open(db)
        await store.append(
            stream_id,
            -1,
            [Event("RunCreated", {"agent": "ollama-chat", "definition_version": "1"})],
        )
        produced = agent.handle_command(
            "message", {"text": "Reply with exactly: AGENTLOG_OK"}
        )
        await store.append(stream_id, 0, produced)

        reactions = DurableDispatcher(
            agent=runtime.agent, store=store, subscription_name="ollama-chat:1:reactions"
        )
        for _ in range(10):
            if not await reactions.run_once():
                break

        crashing_store = CrashOnceStore(store)
        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=crashing_store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="ollama-chat:1:effects",
        )
        crashed = False
        try:
            # RunCreated/UserMessageAdded have no registered effect
            # (empty-output commits, never crash-injected) -- only
            # ModelCallRequested's commit carries non-empty events, so
            # draining is what makes the crash trigger on the right call.
            for _ in range(10):
                if not await effects.run_once():
                    break
            print("UNEXPECTED: drained without crashing")
        except RuntimeError as error:
            crashed = True
            print("crashed as expected, after a real Ollama call:", error)
        assert crashed, "the crash injection must have fired"

        history_after_crash = [e.event.event_type for e in await store.load(stream_id)]
        print("history after simulated crash:", history_after_crash)
        assert "ModelCallSucceeded" not in history_after_crash, (
            "the crash must mean nothing was committed"
        )

        # "Restart": brand new Agent/AgentRuntime/dispatcher objects, a
        # freshly reopened store -- nothing shared with the crashed
        # generation above.
        agent2 = define_agent()
        runtime2 = agent2.build_runtime(
            context=OllamaContext(model="llama3.2:1b", client=make_client())
        )
        store2 = await SQLiteEventStore.open(db)
        effects2 = DurableEffectDispatcher(
            agent=runtime2.agent,
            store=store2,
            effects=runtime2.effects,
            context=runtime2.context,
            subscription_name="ollama-chat:1:effects",
        )
        for _ in range(10):
            if not await effects2.run_once():
                break

        history_after_restart = await store2.load(stream_id)
        event_types = [e.event.event_type for e in history_after_restart]
        print("history after restart+retry:", event_types)

        succeeded = [
            e for e in history_after_restart if e.event.event_type == "ModelCallSucceeded"
        ]
        assert len(succeeded) == 1, f"expected exactly one committed result, got {len(succeeded)}"
        print("committed ModelCallSucceeded content:", succeeded[0].event.data["content"])
        print(
            "operation_id on committed result:",
            succeeded[0].event.metadata.get("operation_id"),
        )

        request_event = next(
            e for e in history_after_restart if e.event.event_type == "ModelCallRequested"
        )
        print(
            "operation_id on the request (should match):",
            request_event.event.metadata.get("operation_id"),
        )
        assert (
            succeeded[0].event.metadata.get("operation_id")
            == request_event.event.metadata.get("operation_id")
            == str(request_event.event.event_id)
        ), "operation_id must be the stable request event_id, unaffected by the retry"

        print()
        print("total real Ollama HTTP calls observed:", ollama_call_count)
        assert ollama_call_count == 2, (
            "the crash must have caused exactly one duplicate real Ollama call",
            ollama_call_count,
        )

        print()
        print("CONTRACT CONFIRMED: commit retry != effect retry (this is a genuine crash")
        print("simulation, not a commit conflict) -- the crash happened strictly *after* a")
        print("real, successful Ollama call, so Ollama was necessarily invoked twice for this")
        print("run; exactly one result was ever durably committed, correctly identified by")
        print("the stable operation_id derived from the immutable request event.")


if __name__ == "__main__":
    asyncio.run(main())
