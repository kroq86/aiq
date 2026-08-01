from __future__ import annotations

import asyncio

from agentlog.evals import EvalCase, RestartPoint, UnsupportedRestartPoint
from agentlog.trace import CausalTrace, build_causal_trace

from .runtime_harness import RuntimeHarness


BOUNDARIES: dict[str, tuple[str, int]] = {
    "before-first-model-effect": ("ModelCallRequested", 1),
    "after-first-model-result": ("ModelCallSucceeded", 1),
    "before-tool-effect": ("ToolCallRequested", 1),
    "after-tool-result": ("ToolCallSucceeded", 1),
    "before-second-model-effect": ("ModelCallRequested", 2),
    "before-terminal-reaction": ("ModelCallSucceeded", 2),
    "after-terminal-commit": ("RunCompleted", 1),
}


class ModelLoopRestartExecutor:
    async def run_normal(self, case: EvalCase) -> CausalTrace:
        return await asyncio.to_thread(self._execute, None)

    async def restart_points(
        self, case: EvalCase, normal_trace: CausalTrace
    ) -> tuple[RestartPoint, ...]:
        return tuple(RestartPoint(boundary) for boundary in BOUNDARIES)

    async def run_restarted(
        self, case: EvalCase, point: RestartPoint
    ) -> CausalTrace:
        if point.boundary not in BOUNDARIES:
            raise UnsupportedRestartPoint(point.boundary)
        return await asyncio.to_thread(self._execute, point.boundary)

    @staticmethod
    def _execute(restart_boundary: str | None) -> CausalTrace:
        runtime = RuntimeHarness.create()
        restarted = False
        for _ in range(40):
            for action in ("reaction", "effect"):
                runtime.dispatch(action)
                history = runtime.history()
                if restart_boundary is not None and not restarted:
                    event_type, occurrence = BOUNDARIES[restart_boundary]
                    if sum(
                        item.event.event_type == event_type for item in history
                    ) >= occurrence:
                        runtime.restart()
                        restarted = True
                if history[-1].event.event_type == "RunCompleted":
                    if restart_boundary is not None and not restarted:
                        raise AssertionError(
                            f"restart boundary was not reached: {restart_boundary}"
                        )
                    return build_causal_trace(
                        agent_name="assistant",
                        run_id="model-verification",
                        agent=runtime.runtime.agent,
                        history=runtime.history(),
                    )
        raise AssertionError("runtime did not reach RunCompleted")
