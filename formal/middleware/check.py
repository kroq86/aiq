from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass

BOUND = 8


@dataclass(frozen=True, slots=True)
class State:
    history: tuple[str, ...] = ()
    model_invocations: int = 0
    tool_invocations: int = 0
    terminal: bool = False
    response_identity_preserved: bool = True


def successors(state: State, *, mutant: str | None) -> tuple[tuple[str, State], ...]:
    if len(state.history) >= BOUND:
        return ()
    if not state.history:
        failed_calls = 1 if mutant == "before_invokes" else 0
        return (
            ("before_model_ok", State(("ModelCallRequested",))),
            (
                "before_model_fail",
                State(("MiddlewareFailed:before_model",), failed_calls),
            ),
        )
    if state.terminal:
        return (("restart", state),)
    last = state.history[-1]
    if last == "ModelCallRequested":
        return (
            (
                "after_model_ok",
                State(
                    state.history + ("ModelCallSucceeded:tool",),
                    1,
                    response_identity_preserved=mutant != "rewrite_response_identity",
                ),
            ),
            (
                "after_model_fail",
                State(state.history + ("MiddlewareFailed:after_model",), 1),
            ),
        )
    if last == "ModelCallSucceeded:tool":
        failed_calls = 1 if mutant == "before_invokes" else 0
        return (
            (
                "before_tool_ok",
                State(state.history + ("ToolCallRequested",), 1),
            ),
            (
                "before_tool_fail",
                State(
                    state.history + ("MiddlewareFailed:before_tool",),
                    1,
                    failed_calls,
                ),
            ),
        )
    if last == "ToolCallRequested":
        return (
            (
                "after_tool_ok",
                State(state.history + ("ToolCallSucceeded",), 1, 1),
            ),
            (
                "after_tool_fail",
                State(
                    state.history + ("MiddlewareFailed:after_tool",), 1, 1
                ),
            ),
        )
    if last.startswith("MiddlewareFailed:"):
        return (("fail_run", State(state.history + ("RunFailed",), state.model_invocations, state.tool_invocations, True)),)
    if last == "ToolCallSucceeded":
        return (("complete", State(state.history + ("RunCompleted",), 1, 1, True)),)
    return (("restart", state),)


def violation(state: State) -> str | None:
    if "MiddlewareFailed:before_model" in state.history and state.model_invocations:
        return "BeforeModelFailurePreventsInvocation"
    if "MiddlewareFailed:before_tool" in state.history and state.tool_invocations:
        return "BeforeToolFailurePreventsInvocation"
    if "MiddlewareFailed:after_model" in state.history and state.model_invocations != 1:
        return "AfterModelFailureFollowsOneInvocation"
    if "MiddlewareFailed:after_tool" in state.history and state.tool_invocations != 1:
        return "AfterToolFailureFollowsOneInvocation"
    if state.terminal and state.history[-1] not in {"RunFailed", "RunCompleted"}:
        return "TerminalIsAbsorbing"
    if not state.response_identity_preserved:
        return "AfterModelPreservesResponseIdentity"
    return None


def explore(*, mutant: str | None) -> tuple[int, int, tuple[str, ...] | None, str | None]:
    initial = State()
    queue = deque([(initial, ())])
    seen = {initial}
    transitions = 0
    while queue:
        state, path = queue.popleft()
        broken = violation(state)
        if broken:
            return len(seen), transitions, path, broken
        for action, candidate in successors(state, mutant=mutant):
            transitions += 1
            if candidate not in seen:
                seen.add(candidate)
                queue.append((candidate, path + (action,)))
    return len(seen), transitions, None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mutant", choices=("before_invokes", "rewrite_response_identity")
    )
    args = parser.parse_args()
    states, transitions, path, broken = explore(mutant=args.mutant)
    if args.mutant:
        expected = {
            "before_invokes": "BeforeModelFailurePreventsInvocation",
            "rewrite_response_identity": "AfterModelPreservesResponseIdentity",
        }[args.mutant]
        if broken != expected:
            print(f"MUTANT_SURVIVED states={states} transitions={transitions}")
            return 1
        print(
            f"MUTANT_KILLED mutant={args.mutant} property={broken} bound={BOUND} "
            f"states={states} transitions={transitions} path={' -> '.join(path or ())}"
        )
        return 0
    if broken:
        print(f"FAIL property={broken} path={' -> '.join(path or ())}")
        return 1
    print(f"PASS bound={BOUND} states={states} transitions={transitions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
