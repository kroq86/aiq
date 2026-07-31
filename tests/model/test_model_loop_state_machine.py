from __future__ import annotations

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from .normalization import normalize_history
from .reference import assert_invariants, initial_state, step
from .runtime_harness import RuntimeHarness


class ModelLoopMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.reference = initial_state()
        self.previous = self.reference
        self.runtime = RuntimeHarness.create()

    def apply(self, action: str) -> None:
        self.previous = self.reference
        self.reference = step(self.reference, action)
        self.runtime.dispatch(action)

    @rule()
    def dispatch_reaction_once(self) -> None:
        self.apply("reaction")

    @rule()
    def dispatch_effect_once(self) -> None:
        self.apply("effect")

    @rule()
    def model_effect_fails(self) -> None:
        self.apply("effect_model_failure")

    @rule()
    def tool_effect_fails(self) -> None:
        self.apply("effect_tool_failure")

    @rule()
    def restart_runtime(self) -> None:
        self.apply("restart")

    @precondition(lambda self: not self.reference.terminal)
    @rule()
    def force_terminal(self) -> None:
        self.apply("force_terminal")

    @invariant()
    def runtime_matches_reference(self) -> None:
        assert_invariants(self.previous, self.reference)
        assert normalize_history(self.runtime.history()) == self.reference.history
        assert self.runtime.checkpoints() == (
            self.reference.reaction_checkpoint,
            self.reference.effect_checkpoint,
        )

    @invariant()
    def state_is_fold_of_history(self) -> None:
        rebuilt = self.runtime.runtime.agent.rebuild(self.runtime.history())
        assert rebuilt.answer == self.reference.answer


ModelLoopStateMachineTests = ModelLoopMachine.TestCase
ModelLoopStateMachineTests.settings = settings(
    max_examples=40,
    stateful_step_count=35,
    deadline=None,
)
