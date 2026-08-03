# v0.4 cycle-guard bounded model

This local finite model covers only the repeated-workflow-state guard
(`_cycle_failure` in `src/agentlog/model_loop.py`), reusing the
`low -> low | before -> at` nondeterministic counter abstraction already
established for `ModelClass`/`ToolClass` in `formal/FORMAL_MODEL.md` Sec. 4.
It deliberately excludes the goal/invariant completion gate, tool
argument/result content, provider semantics, persistence, and concurrency.

```bash
python3 formal/cycle_guard/check.py
python3 formal/cycle_guard/check.py --mutant disable_cycle_guard
python3 formal/cycle_guard/check.py --mutant cycle_allows_completion
```

The normal model checks that a blocked repeat (`at` class) never reaches
`RunCompleted`, that `WorkflowCycleDetected` is always followed by
`RunFailed`, that a terminal state is absorbing, and -- the reason this model
exists -- that `WorkflowCycleDetected` is *reachable* at all
(`cycle_detected_witnessed=True` in normal output; the check fails as
`VACUOUS` otherwise). The two targeted mutants disable the guard at the `at`
class and let a detected cycle reach `RunCompleted`; the unchanged checker
kills both with a counterexample path.

## What this closes, and what it does not

Before this model, `formal/model/spec.py`'s two `WorkflowCycleDetected`
assertions were vacuous: no action in that reference model, and no test in
this repository, ever constructed a state containing that event
(`NOTE(vacuity)` in `formal/model/spec.py`, boundary decision B in
`formal/FORMAL_MODEL.md` Sec. 2.1). This model does not change that -- the
trace/bisimulation reference model used for runtime refinement is untouched,
and its `WorkflowCycleDetected` assertions remain vacuous exactly as
documented there.

What this model adds is independent, standalone bounded-exhaustive evidence
that the cycle guard's *own* abstract safety properties hold and that
`WorkflowCycleDetected` is reachable in *some* checked model, with killed
targeted mutants -- the same tier of evidence `formal/middleware/` and
`formal/sequence/` already provide for their own local pieces.

Not established by this model:

- a refinement/abstraction mapping from the real, JSON-normalized,
  unbounded-domain `_fingerprint_snapshot` mechanism down to this model's
  three classes -- there is no `beta: Concrete -> Abstract` argument here,
  unlike `formal/FORMAL_MODEL.md` Sec. 5's mapping for the base model-loop
  counters;
- anything about `GoalSatisfied`/`GoalNotSatisfied`/
  `WorkflowInvariantViolated`/`RunAbstained` -- those are outside this model.
  The first three have a separate local model in `formal/completion_gate/`;
  abstention routing has one in `formal/run_abstained/`;
- universal runtime refinement of `_cycle_failure` -- see
  `tests/test_v04_constrained_execution_e2e.py`'s
  `V04ControlRestartEquivalenceTests` for the separate, scenario-level
  runtime evidence for that function.

This is bounded exhaustive evidence for this finite abstraction, not
universal runtime refinement.
