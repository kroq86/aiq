import asyncio
import unittest
from dataclasses import dataclass

from aiq import (
    AgentDefinition,
    DurableDispatcher,
    DurableEffectDispatcher,
    EffectContext,
    EffectRegistry,
    Event,
    InMemoryEventStore,
    effect_request,
    run_stream_id,
)


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class State:
    pass


def reaction_agent(name: str) -> AgentDefinition[State]:
    agent = AgentDefinition(name, initial_state=State)

    @agent.reducer
    def evolve(state: State, event: Event) -> State:
        return state

    @agent.react("InputAdded")
    def react(event: Event, state: State):
        return [Event(f"{name}Reacted", {})]

    return agent


class AgentIsolationTests(unittest.TestCase):
    def test_each_reaction_dispatcher_only_writes_to_its_own_stream(self) -> None:
        async def scenario() -> None:
            store = InMemoryEventStore()
            await store.append(
                run_stream_id("agent-b", "run-1"),
                -1,
                [Event("InputAdded", {})],
            )
            await store.append(
                run_stream_id("agent-a", "run-1"),
                -1,
                [Event("InputAdded", {})],
            )
            workers = [
                DurableDispatcher(
                    agent=reaction_agent(name),
                    store=store,
                    subscription_name=f"{name}:reactions",
                )
                for name in ("agent-a", "agent-b")
            ]

            for _ in range(20):
                progress = [await worker.run_once() for worker in workers]
                if not any(progress):
                    break
            else:
                self.fail("reaction workers did not reach idle")

            a_types = [
                item.event.event_type
                for item in await store.load(
                    run_stream_id("agent-a", "run-1")
                )
            ]
            b_types = [
                item.event.event_type
                for item in await store.load(
                    run_stream_id("agent-b", "run-1")
                )
            ]
            self.assertEqual(a_types, ["InputAdded", "agent-aReacted"])
            self.assertEqual(b_types, ["InputAdded", "agent-bReacted"])

            latest_position = (await store.load_global())[-1].global_position
            self.assertEqual(
                await store.load_checkpoint("agent-a:reactions"),
                latest_position,
            )
            self.assertEqual(
                await store.load_checkpoint("agent-b:reactions"),
                latest_position,
            )
            self.assertEqual(
                [await worker.run_once() for worker in workers],
                [False, False],
            )

        run(scenario())

    def test_foreign_effect_advances_checkpoint_without_invoking_adapter(self) -> None:
        async def scenario() -> None:
            store = InMemoryEventStore()
            foreign = effect_request("ExternalRequested", {})
            owned = effect_request("ExternalRequested", {})
            await store.append(
                run_stream_id("agent-b", "run-1"),
                -1,
                [foreign],
            )
            await store.append(
                run_stream_id("agent-a", "run-1"),
                -1,
                [owned],
            )

            agent = reaction_agent("agent-a")
            effects = EffectRegistry[State]()
            calls: list[str] = []

            @effects.effect("ExternalRequested")
            async def execute(
                event: Event,
                state: State,
                context: EffectContext,
            ):
                calls.append(str(event.event_id))
                return [Event("ExternalSucceeded", {})]

            worker = DurableEffectDispatcher(
                agent=agent,
                store=store,
                effects=effects,
                context=EffectContext({}),
                subscription_name="agent-a:effects",
            )

            self.assertIs(await worker.run_once(), True)
            self.assertEqual(calls, [])
            self.assertEqual(
                await store.load_checkpoint("agent-a:effects"),
                1,
            )

            for _ in range(10):
                if not await worker.run_once():
                    break
            self.assertEqual(calls, [str(owned.event_id)])
            self.assertNotIn(str(foreign.event_id), calls)
            self.assertEqual(
                [
                    item.event.event_type
                    for item in await store.load(
                        run_stream_id("agent-b", "run-1")
                    )
                ],
                ["ExternalRequested"],
            )
            self.assertEqual(
                [
                    item.event.event_type
                    for item in await store.load(
                        run_stream_id("agent-a", "run-1")
                    )
                ],
                ["ExternalRequested", "ExternalSucceeded"],
            )

            calls_at_idle = len(calls)
            self.assertEqual(
                [await worker.run_once() for _ in range(5)],
                [False] * 5,
            )
            self.assertEqual(len(calls), calls_at_idle)

        run(scenario())


if __name__ == "__main__":
    unittest.main()
