# RunAbstained bounded model

This standalone finite model covers only the durable reaction to an already
committed `ToolValidationFailed` event:

```text
status=abstain -> RunAbstained
status=fail    -> RunFailed
```

The validation phase and decision are independent axes:

```text
(request | result) x (abstain | fail)
```

`request` represents input, transition, or request validation before tool
execution. `result` represents output/result validation after request
acceptance and possible physical tool execution; its history therefore retains
`ToolValidationSucceeded(request)` before the failed result validation.

## Reproduction

```bash
python3 formal/run_abstained/check.py
python3 formal/run_abstained/check.py \
  --mutant abstain_routes_to_run_failed
python3 formal/run_abstained/check.py \
  --mutant fail_routes_to_run_abstained
python3 formal/run_abstained/check.py \
  --mutant abstain_reaches_completion
python3 formal/run_abstained/check.py \
  --mutant duplicate_terminal
python3 formal/run_abstained/check.py \
  --mutant terminal_not_absorbing
```

Normal result:

```text
PASS bound=2 states=8 transitions=4 cases=4 ... terminal_deadlocks=4
```

All five targeted mutants are killed by the unchanged checker with concrete
paths:

- abstain is incorrectly routed to `RunFailed`;
- fail is incorrectly routed to `RunAbstained`;
- abstention incorrectly reaches `RunCompleted`;
- a second terminal event is committed;
- an event appears after a terminal event.

## Evidence report

Protocol scope:
`DurableModelLoop.handle_validation_failure` routing after durable validation
failure. Creation of the failure event and policy evaluation are excluded.

Formally established:
within this finite abstraction, abstain and fail have distinct terminal
outcomes, abstention cannot complete, each history has at most one terminal,
and terminal states are absorbing.

Bounds/domain and state/transition counts:
transition bound 2; phase domain `{request, result}`; decision domain
`{abstain, fail}`; 8 reachable normalized states and 4 transitions.

Non-vacuity and deadlocks:
all four phase/decision cases, both terminal events, both request-side failure
events, both result-side failure events, and the accepted-request prefix are
witnessed. The four deadlocks are exactly the four terminal outcomes.

Mutants killed/survived/equivalent/invalid:
5 killed, 0 survived, 0 equivalent, 0 invalid.

Runtime scenarios and unmatched boundaries:
existing constrained-execution tests cover request-side abstention and terminal
registration; bounded-workflow tests cover result-side abstention. No explicit
refinement mapping connects arbitrary runtime observations to this model.

Composition obligations open:
composition with the base lifecycle, completion-gate, cycle-guard, effect
dispatcher, and crash-window models is not established.

Liveness/fairness established or open:
no scheduler or environment liveness property is modeled. Local routing has no
unexpected nonterminal deadlock within the bound.

Trusted computing base:
Python, the BFS implementation, the state encoding, property predicates, test
runner, and the reviewer's interpretation of the runtime abstraction.

Not proved:

- that `ToolValidationFailed` was created from the correct policy decision;
- correctness or calibration of an application's abstention policy;
- distinct retry/replan workflow semantics;
- whether a result-side physical effect ran once or can safely be repeated;
- persistence, concurrency, crash safety, or exactly-once execution;
- universal runtime refinement or composition with other local models.

This is bounded-exhaustive local evidence, not a proof of the implementation or
of abstention quality.
