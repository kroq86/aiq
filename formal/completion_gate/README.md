# v0.4 completion-gate bounded model

This standalone finite model covers only the final-answer gate in
`DurableModelLoop.interpret_model`:

```text
configured invariant fails -> WorkflowInvariantViolated -> RunFailed
invariant passes, configured goal fails -> GoalNotSatisfied -> RunFailed
invariant passes, configured goal passes
  -> GoalSatisfied -> AnswerProduced -> RunCompleted
both predicates absent -> AnswerProduced -> RunCompleted
```

The invariant and goal configuration axes are independent. The checker starts
from all nine meaningful predicate cases:

```text
(invariant absent | configured false | configured true)
x
(goal absent | configured false | configured true)
```

Outcome states discard predicate values that the runtime no longer evaluates.
This quotient yields exactly 15 reachable normalized states and 11
transitions at bound 3.

## Reproduction

```bash
python3 formal/completion_gate/check.py
python3 formal/completion_gate/check.py --mutant invariant_allows_completion
python3 formal/completion_gate/check.py --mutant goal_allows_completion
python3 formal/completion_gate/check.py --mutant completion_before_goal
python3 formal/completion_gate/check.py --mutant goal_checked_before_invariant
python3 formal/completion_gate/check.py --mutant terminal_not_absorbing
```

Normal result:

```text
PASS bound=3 states=15 transitions=11 configured_cases=4
witnessed_events=GoalNotSatisfied,GoalSatisfied,RunCompleted,RunFailed,WorkflowInvariantViolated
terminal_deadlocks=4
```

All five mutants are killed by the unchanged checker with concrete paths:

- invariant violation reaches completion;
- an unsatisfied goal reaches completion;
- completion precedes `GoalSatisfied`;
- goal is evaluated before a known invariant failure;
- an event appears after a terminal event.

## Evidence boundary

This establishes, within the explicit finite abstraction:

- all four configured/not-configured combinations are represented;
- all material gate events and both terminal outcomes are reachable;
- invariant failure preempts goal evaluation;
- invariant or goal failure cannot complete;
- configured goal success precedes completion;
- the only deadlocks are the four normalized terminal outcomes;
- terminal states are absorbing.

It does not modify `formal/model/spec.py`; that trace/reference model's
goal-event assertions remain vacuous. It also does not establish:

- a universal refinement mapping from arbitrary Python predicates to these
  boolean classes;
- correctness of an application's invariant or goal predicate;
- persistence, concurrency, liveness, or scheduler fairness;
- cycle detection, abstention routing, tool execution, or provider behavior;
- composition with the base lifecycle, cycle-guard, or RunAbstained model.

`RunAbstained` routing is covered separately by
`formal/run_abstained/`; it is not part of this checker.

This is bounded-exhaustive local evidence, not a proof of the implementation
or of business-goal truth.
