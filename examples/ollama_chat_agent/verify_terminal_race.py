"""Manual integration check (needs a running local Ollama -- not part of
`tests/`): a real, in-flight Ollama call is artificially held after it
actually responds (to make the race window reliable instead of hoping a
fast local model is slow) while the run is forced terminal through a
different path. The late-arriving real result must not be committed.

Run:

    PYTHONPATH=src:. python examples/ollama_chat_agent/verify_terminal_race.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx

from agentlog import DurableDispatcher, DurableEffectDispatcher, Event, SQLiteEventStore
from examples.ollama_chat_agent.main import OllamaContext, define_agent


async def _slow_down_response(response: httpx.Response) -> None:
    # The Ollama call itself is 100% real; this only delays handing the
    # (real, already-received) response back to the caller, to make the
    # race window reliable instead of hoping a fast local model is slow.
    await response.aread()
    print("  >>> real Ollama response received, artificially holding it for 2s...", flush=True)
    await asyncio.sleep(2.0)


async def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "terminal-race.db"

        client = httpx.AsyncClient(timeout=60.0, event_hooks={"response": [_slow_down_response]})
        agent = define_agent()
        runtime = agent.build_runtime(
            context=OllamaContext(model="llama3.2:1b", client=client)
        )
        stream_id = "ollama-chat:race-run"

        store = await SQLiteEventStore.open(db)
        await store.append(
            stream_id, -1,
            [Event("RunCreated", {"agent": "ollama-chat", "definition_version": "1"})],
        )
        produced = agent.handle_command("message", {"text": "Reply with exactly: AGENTLOG_OK"})
        await store.append(stream_id, 0, produced)

        reactions = DurableDispatcher(
            agent=runtime.agent, store=store, subscription_name="ollama-chat:1:reactions"
        )
        for _ in range(10):
            if not await reactions.run_once():
                break

        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="ollama-chat:1:effects",
        )

        async def run_effect_dispatch():
            print("starting effect dispatch (real Ollama call in flight)...", flush=True)
            # RunCreated/UserMessageAdded have no registered effect --
            # drain past them (near-instant, no HTTP call) so this
            # actually reaches ModelCallRequested's real Ollama call.
            for _ in range(10):
                if not await effects.run_once():
                    break
            print("effect dispatch finished", flush=True)

        async def force_terminal_midflight():
            await asyncio.sleep(0.5)
            print("forcing RunCompleted via a different path while Ollama is in flight...", flush=True)
            racing_store = await SQLiteEventStore.open(db)
            history = await racing_store.load(stream_id)
            await racing_store.append(
                stream_id, history[-1].stream_version, [Event("RunCompleted", {})]
            )
            print("RunCompleted forced in", flush=True)

        await asyncio.gather(run_effect_dispatch(), force_terminal_midflight())

        history = await store.load(stream_id)
        event_types = [e.event.event_type for e in history]
        print("final history:", event_types, flush=True)

        assert event_types[-1] == "RunCompleted", (
            "RunCompleted must remain the last meaningful event", event_types
        )
        assert "ModelCallSucceeded" not in event_types, (
            "the late real Ollama result must not be committed after terminal", event_types
        )
        terminal_count = sum(1 for t in event_types if t in ("RunCompleted", "RunFailed"))
        assert terminal_count == 1

        checkpoint = await store.load_checkpoint("ollama-chat:1:effects")
        print("effects checkpoint after the race:", checkpoint, "(must have advanced, not stuck)", flush=True)
        assert checkpoint > 0

        print(flush=True)
        print("CONTRACT CONFIRMED: a real, in-flight Ollama response that arrives after the", flush=True)
        print("run has already become terminal via a different path is discarded, not", flush=True)
        print("committed -- RunCompleted stays the last event, terminal absorption holds", flush=True)
        print("even for a genuinely slow real external call, not just a fake instant one.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
