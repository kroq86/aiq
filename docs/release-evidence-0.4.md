# Agentlog v0.4 constrained-execution evidence

## Protocol scope

This gate exercises the currently implemented v0.4 slice through the real
`Agent`, reaction dispatcher, effect dispatcher, model loop, application
policy, executable tool, durable events, replay, and restart reconstruction.
The provider and domain policy are deterministic in-process test components;
no network or real LLM is involved.

## Executed scenarios

```text
accepted transition:
  request evidence -> tool execution -> result evidence -> ToolCallSucceeded
  -> RunCompleted

irrelevant request:
  non-retryable request validation failure -> no ToolCallSucceeded -> RunFailed

ambiguous selection:
  candidates recorded -> retryable validation failure -> new model request
  -> no execution of rejected call -> RunCompleted

postcondition failure:
  request evidence -> physical tool invocation -> non-retryable result failure
  -> no committed ToolCallSucceeded -> RunFailed

goal gate:
  false goal -> GoalNotSatisfied -> no RunCompleted -> RunFailed
  true goal -> durable GoalSatisfied -> RunCompleted

workflow invariant:
  failed application invariant -> WorkflowInvariantViolated -> RunFailed

cycle boundary:
  repeated normalized workflow state -> second tool blocked -> RunFailed

unified decisions:
  abstain -> distinct RunAbstained terminal
  accept normalized input -> executable tool receives trusted normalized value
  accept normalized output -> only verified value enters ToolCallSucceeded
```

Restart-boundary coverage by scenario, as actually implemented in
`tests/test_v04_constrained_execution_e2e.py`:

```text
accepted / irrelevant / ambiguous / postcondition failure
  -> V04ConstrainedExecutionEndToEndTests.assert_restart_equivalent()
  -> normal vs restart-after-every-dispatch, full normalized history compared

GoalNotSatisfied, WorkflowInvariantViolated, WorkflowCycleDetected
  -> V04ControlRestartEquivalenceTests (added in this hardening pass)
  -> assert_control_restart_equivalent(): runtime + both dispatchers rebuilt
     from the persisted store before and after every reaction/effect call;
     full normalized history compared normal vs restart; single external
     tool call asserted; RunCompleted asserted absent; exactly one of
     RunCompleted/RunFailed/RunAbstained asserted present

GoalSatisfied, RunAbstained
  -> tests/test_v04_bounded_workflow_e2e.py
     (test_bounded_happy_path_is_restart_equivalent,
      test_irrelevant_or_unsupported_results_abstain), same rebuild pattern
```

All five v0.4 completion/control event types (`GoalSatisfied`,
`GoalNotSatisfied`, `WorkflowInvariantViolated`, `WorkflowCycleDetected`,
`RunAbstained`) now have committed restart-equivalence coverage. None of this
is compared against the pure reference model in `formal/model/spec.py` for
these five event types -- see "Reference-model boundary" below.

## Targeted mutation evidence

Manual source mutants applied to `src/agentlog/model_loop.py`, run against
the committed suite, observed to fail, then reverted (verified back to a
clean `git diff` and a full pass afterward). These are targeted-adequacy
checks for five specific v0.4 safety claims, not a mutation-completeness
claim.

| # | Mutant | Expected violated property | Test that kills it | Result |
|---|---|---|---|---|
| 1 | Disable the goal gate (`if False and goal_satisfied is not None and not goal_satisfied(state):`) at `model_loop.py` `interpret_model` | `GoalPolicyConfigured ∧ ¬goal_satisfied(state) ⇒ RunCompleted` must not occur | `test_false_goal_prevents_successful_completion`, `V04ControlRestartEquivalenceTests.test_goal_not_satisfied_is_restart_equivalent_and_terminal` | killed |
| 2 | On goal failure, emit `(GoalNotSatisfied, AnswerProduced, RunCompleted)` instead of `GoalNotSatisfied` alone | `GoalNotSatisfied ⇒ ¬RunCompleted` | same two tests as #1 | killed |
| 3 | Force `cycle_reason = None` unconditionally in `interpret_model`, disabling `_cycle_failure` | repeated-state guard must stop a second forbidden tool execution | `test_workflow_cycle_detected_is_restart_equivalent_and_terminal` (tool called 6x instead of once; no `WorkflowCycleDetected`) | killed |
| 4 | Re-route `WorkflowCycleDetected` to `agent.react(...)(lambda state, event: events.RunCompleted())` instead of the shared `RunFailed` failure-type loop | `WorkflowCycleDetected ⇒ ¬RunCompleted` | `test_workflow_cycle_detected_is_restart_equivalent_and_terminal` | killed |
| 5 | Remove `agent.terminal(events.RunAbstained, status="abstained")` | `RunAbstained` must be registered as a terminal status (closes the run, single committed outcome) | `test_run_abstained_is_registered_as_a_terminal_status` (new); confirmed the two pre-existing `RunAbstained` tests do **not** catch this mutant (survive) | killed by the new test only |

These five mutants were originally applied and reverted by hand; that is not
independently reproducible by another developer from prose alone. They are
now encoded as a runnable manifest,
`formal/model/verify_v04_runtime_mutants.py`: it snapshots the current
(uncommitted) `src/agentlog/model_loop.py` content in memory, applies one
exact string-replacement mutation, runs the exact `pytest -k` command for
that mutant, and restores the original content from the in-memory snapshot
in a `finally` block -- never via `git checkout`, since this file has
uncommitted changes that a git-based restore would discard. It verifies the
restore by re-reading the file and raises if it does not match. This is
deliberately not the project's existing FASM/setdb `verify-mutants`
convention (`formal/*/verify-mutants`), which mutates a compiled pure model
via a `--mutant` variant with no file patching; there is no equivalent
mechanism for the real, compiled runtime, so this script does the safe
version of literal source patching instead.

```bash
PYTHONPATH=src:. python formal/model/verify_v04_runtime_mutants.py
```

```text
MUTANT_KILLED mutant=goal_gate_disabled ...
RESTORE_VERIFIED mutant=goal_gate_disabled
MUTANT_KILLED mutant=goal_not_satisfied_still_completes ...
RESTORE_VERIFIED mutant=goal_not_satisfied_still_completes
MUTANT_KILLED mutant=cycle_detection_disabled ...
RESTORE_VERIFIED mutant=cycle_detection_disabled
MUTANT_KILLED mutant=cycle_detected_still_completes ...
RESTORE_VERIFIED mutant=cycle_detected_still_completes
MUTANT_KILLED mutant=run_abstained_not_terminal ...
RESTORE_VERIFIED mutant=run_abstained_not_terminal
V04_RUNTIME_MUTATION_MATRIX mutants=5 all_killed=True
```
Exit code `0`. Verified in this environment: `git diff --stat
src/agentlog/model_loop.py` before and after running the script are
byte-identical.

Mutant 5 is the important negative result here: the suite as it stood before
this hardening pass would not have detected `RunAbstained` losing its
terminal status. `test_run_abstained_is_registered_as_a_terminal_status`
closes that gap by asserting directly on
`AgentRuntime.agent.terminal_status_by_event_type` rather than on event
ordering.

Not attempted / explicitly out of scope for this pass: mutants against
`WorkflowInvariantViolated`'s gate placement relative to the goal check,
mutants against the legacy `ToolPolicy` code path, and any mutant requiring
changes to `formal/model/spec.py` (see next section).

## Reference-model boundary

`formal/model/spec.py` (the pure reference model used for bisimulation
against the real runtime) does **not** model `GoalSatisfied`,
`GoalNotSatisfied`, `WorkflowInvariantViolated`, `WorkflowCycleDetected`, or
`RunAbstained` as actions. Two ordering/exclusion assertions referencing
`GoalSatisfied`/`GoalNotSatisfied`/`WorkflowCycleDetected` were added to
`assert_invariants` in an earlier pass; this hardening pass confirmed by
`grep` across `tests/model/` that no test constructs a `ReferenceState`
containing any of these five event types, so those two assertions are
currently vacuous for every bounded exploration and every committed
reference-model test. They are now marked `NOTE(vacuity)` in
`formal/model/spec.py` and documented as boundary decision **B** in
`formal/FORMAL_MODEL.md` Sec. 2.1: extending the bounded FASM/setdb
exploration to this vocabulary is real modeling work (new abstract actions,
new state space, new saturation counts) and is out of scope for a
release-hardening pass. Runtime-level coverage of the same properties is
scenario-based (restart-equivalence tests above, mutation table above), not
a reference-model proof.

### Cycle-guard bounded model (partial closure)

Following the feasibility spike below, the smallest slice
(`CycleGuardModel`) was actually built as a standalone check, separate from
`spec.py`: `formal/cycle_guard/check.py`. It does not extend or modify
`spec.py`, and it does not establish a refinement mapping from the real
`_fingerprint_snapshot` mechanism to its three abstract classes -- that
mapping work remains open, same as before. What it does add:
`WorkflowCycleDetected` is reachable (non-vacuous) in a checked
bounded-exhaustive exploration, with two killed targeted mutants.

```bash
python3 formal/cycle_guard/check.py
# -> PASS bound=10 states=30 transitions=41 cycle_detected_witnessed=True

python3 formal/cycle_guard/check.py --mutant disable_cycle_guard
# -> MUTANT_KILLED mutant=disable_cycle_guard property=AtClassNeverProceedsToToolCall ...

python3 formal/cycle_guard/check.py --mutant cycle_allows_completion
# -> MUTANT_KILLED mutant=cycle_allows_completion property=CycleDetectedNeverPrecedesRunCompleted ...
```

`GoalSatisfied`, `GoalNotSatisfied`, `WorkflowInvariantViolated`, and
`RunAbstained` are unaffected by this and remain exactly as described above:
vacuous in `spec.py`, covered only by runtime scenario/restart/mutation
evidence, no bounded model. See `formal/cycle_guard/README.md` for the exact
scope boundary.

### Feasibility spike (not committed, not a proof)

A throwaway, out-of-repo spike estimated the state-space cost of closing this
gap, to turn "real modeling work" into a number instead of a hand-wave. It is
not part of this repository and produces no checked evidence; it is
arithmetic over the same abstraction technique already used by the existing
54-state `ModelLoopModel` (`Phase x ModelClass x ToolClass`,
`FORMAL_MODEL.md` Sec. 4).

```text
existing ModelLoopModel (Phase x ModelClass x ToolClass):        54 raw tuples
one merged product adding Goal/Invariant/Cycle/Abstain classes: 3024 raw tuples (56x)
  -- after a rough, unchecked reachability filter:               1134 states
decomposed into 3 small local models (this project's existing pattern
of many small models rather than one product, FORMAL_MODEL.md Sec. 1):
  base lifecycle + 1 absorbing "abstained" phase:                  63 states (+9)
  separate CompletionGateModel (goal x invariant, gated at the
  final-answer boundary only):                                     15 states
  separate CycleGuardModel (reuses the existing nondeterministic
  low/before/at counter technique verbatim):                        3 states
  decomposed total:                                                81 states
```

**Verdict: Moderate extension.** Not small -- it requires at least two new
local FASM/setdb models (a completion gate and a cycle guard), new
saturation/mutant counts for each, one new absorbing phase on the existing
54-state model, new composition-obligation entries connecting them to the
existing lifecycle model, and -- the actual source of "moderate" rather than
"small" -- a new abstraction/refinement mapping from the real
JSON-normalized, unbounded-domain cycle-fingerprint mechanism
(`_fingerprint_snapshot` in `model_loop.py`) down to the 3-class
`CycleGuardModel`, which needs its own soundness argument the way the
existing `beta: Concrete -> Abstract` mapping in `FORMAL_MODEL.md` Sec. 5
does. Not an architectural redesign -- it fits the project's existing
"many small local models + composition obligations" pattern
(`FORMAL_MODEL.md` Sec. 1) without touching `Run_t`, `EventStoreModel`,
`DispatcherModel`, or `CompositionModel`, and reuses the existing
nondeterministic-counter abstraction technique rather than inventing a new
one. The naive single-merged-product approach (1134+ states) is explicitly
the wrong shape per the project's own stated modeling philosophy and should
not be attempted even as a "quick" option.

## Crash-window scope (v0.4 policy hooks)

The pre-existing crash-window model (`formal/FORMAL_MODEL.md` Sec. 6) already
establishes that a crash between physical tool invocation and durable result
commit may cause the effect to be invoked again on restart, with at most one
committed result. v0.4 adds `validate_input`, `capture_pre_state`, and
`validate_output` (and, on the legacy path, `validate_request`/
`validate_result`) as additional calls inside that same
`@agent.effect(ToolCallRequested)` handler, alongside `tool.execute`. All of
them sit inside the same crash window as the tool call itself: a restart
between the external side effect and the durable commit of its outcome can
cause any of `validate_input`, `capture_pre_state`, `tool.execute`, and
`validate_output` to run again. Agentlog's guarantee here is durable
orchestration -- at most one committed result per request -- not
exactly-once execution of external systems. Making any of these hooks safe
under retry requires the same tools already implied by the pre-v0.4 model:
an idempotency key, an idempotent tool/hook implementation, a transactional
integration, or application-level deduplication. This does not change any
v0.4 production semantics; it is a documentation clarification of an
existing, already-proven limitation extended to three new call sites.

## Reproduction

Each command below was run against this working tree with
`PYTHONPATH=src:.` from the repository root, Python 3.14.6, pytest 9.0.3,
ruff 0.15.12.

```bash
# cycle-guard bounded model (no setdb dependency)
python3 formal/cycle_guard/check.py
python3 formal/cycle_guard/check.py --mutant disable_cycle_guard
python3 formal/cycle_guard/check.py --mutant cycle_allows_completion
# -> PASS ... cycle_detected_witnessed=True; both mutants MUTANT_KILLED
```

```bash
# constrained v0.4 E2E
PYTHONPATH=src:. pytest -q tests/test_v04_constrained_execution_e2e.py
# -> 15 passed, 4 subtests passed
```

```bash
# bounded corporate workflow
PYTHONPATH=src:. pytest -q tests/test_v04_bounded_workflow_e2e.py
# -> 5 passed
```

```bash
# both v0.4 E2E gates
PYTHONPATH=src:. pytest -q \
  tests/test_v04_constrained_execution_e2e.py \
  tests/test_v04_bounded_workflow_e2e.py
# -> 20 passed, 4 subtests passed
```

```bash
# policy and formal/model tests (fastapi/hypothesis modules excluded, see below)
PYTHONPATH=src:. pytest -q \
  tests/test_model_loop_policy.py \
  tests/model/ \
  --ignore=tests/model/test_fastapi_semantic_equivalence.py \
  --ignore=tests/model/test_model_loop_state_machine.py
# -> 18 passed, 2 skipped, 5 subtests passed
```

```bash
# lint, exact changed/added file set (git diff --name-only + git status --porcelain '??')
ruff check \
  docs/model-loop.md docs/positioning.md \
  formal/FORMAL_MODEL.md formal/model/spec.py \
  src/agentlog/__init__.py src/agentlog/model_loop.py src/agentlog/validation.py \
  tests/model/normalization.py tests/model/runtime_harness.py \
  tests/model/test_fastapi_semantic_equivalence.py tests/model/test_invariants.py \
  tests/model/test_runtime_equivalence.py tests/test_model_loop_policy.py \
  tests/test_v04_bounded_workflow_e2e.py tests/test_v04_constrained_execution_e2e.py \
  docs/release-evidence-0.4.md
# -> All checks passed!
```

```bash
# whitespace/diff hygiene for already-tracked files
git diff --check
# -> exit 0, no output
# NOTE: git diff --check only inspects tracked-file diffs; it does not cover
# the four untracked files above (docs/release-evidence-0.4.md,
# src/agentlog/validation.py, tests/test_v04_bounded_workflow_e2e.py,
# tests/test_v04_constrained_execution_e2e.py).
```

Removed: an earlier "selected regression/model suite: 76 passed, 12 subtests
passed" line was not reproducible from any single command recorded in this
document and has been replaced by the exact, individually-reproducible
commands above. If a broader regression run is wanted, run each command
separately and report each count against its exact command -- do not merge
counts from different invocations into one unlabeled number.

`ruff check .` (whole repository, no path filter) currently reports errors in
files this v0.4 candidate does not touch (e.g. `tests/test_runtime.py`,
`src/agentlog/llm.py`, `tests/model/reference.py`); those are pre-existing
and out of scope for this candidate.

## Post-merge release verification

The implementation candidate base is commit
`b7cc14e77021625a7e453935157e6adb73bbc50b`. Release verification was run
from a clean `main` synchronized with `origin/main`, using Python 3.14.6,
pytest 9.1.1, and the declared lint contract Ruff 0.15.12.

```bash
python -m pip install -e ".[test,test-fastapi,ollama,lint]"
python -m pytest -q -rs
# -> 290 passed, 2 skipped, 57 subtests passed
```

The two skips are the bounded formal tests that require the external `setdb`
binary; `setdb` is not a Python package dependency and belongs to the separate
formal environment. The focused v0.4 Ruff gate passed, all five runtime
mutants were killed and independently restore-verified, and the installed
wheel smoke passed the legacy `ToolPolicy`, new `ExecutionPolicy` goal gate,
and `RunAbstained` terminal paths. Package metadata for this release is
`0.4.0`.

### Release boundaries

This release does not establish an exhaustive bounded proof for the expanded
v0.4 control-event vocabulary. It does not guarantee exactly-once physical
execution, provide a real MCP adapter or production RAG implementation, or
claim universal prompt-injection protection. The formal boundary and crash
window are documented above; the v0.4 evidence for the new control events is
runtime scenario, restart-equivalence, invariant, and targeted mutation
evidence.

### Release artifacts

Built with `python -m build` 1.5.0 using the declared Hatchling backend:

```text
bf22b7ad598b9a19f078ee5c230b47a9a2fa6fdda550e68f1cbad1c5a9de373a  agentlog-0.4.0.tar.gz
69cc644139ddce2164e5cedc0b2842cf5cfaf435dff782f38001cbc985079455  agentlog-0.4.0-py3-none-any.whl
```

Release-evidence documents are excluded from distributions so an sdist hash
can be recorded here without creating a self-referential artifact. The wheel
contains `agentlog/validation.py` and excludes `tests/`, `formal/`, and
`scripts/`.

## Bounded corporate workflow gate

`tests/test_v04_bounded_workflow_e2e.py` treats the model as a bounded
classifier/action selector inside a deterministic invoice workflow. It
checks:

```text
allowed intent/tool -> tenant guard -> result boundary -> sanitized facts
  -> durable GoalSatisfied -> RunCompleted

wrong tenant -> transition rejected before tool execution

low relevance or unsupported language -> RunAbstained

delete_records proposed with trusted_evidence=false
  -> rejected before tool execution

application-owned TrustedReviewApproved event
  -> reducer sets trusted_evidence=true
  -> value enters the durable workflow snapshot
  -> restart reconstructs the same value
  -> the same privileged transition is admitted and invoked once
```

The happy path is compared normal versus restart-after-every-dispatch. The
three rejection/abstention paths also run with restart after every dispatcher
boundary.

```bash
PYTHONPATH=src:. pytest -q tests/test_v04_bounded_workflow_e2e.py
# -> 5 passed
```

This is one representative enterprise architecture: a small model selects
from an allowlisted action set while application policies enforce tenant
isolation, admissible results, abstention, and goal-gated completion. It does
not prove universal prompt-injection detection, arbitrary workflow
correctness, or planning reliability.

The privileged-action guard (`InvoiceExecutionPolicy.validate_transition`)
reads `context.workflow_state["trusted_evidence"]`, a real field threaded
through the durable, JSON-normalized workflow snapshot. The negative test
`test_privileged_transition_requires_trusted_workflow_evidence` proves that
the privileged tool is not invoked while that field is false. The positive
test
`test_application_trusted_review_allows_privileged_transition_after_restart`
creates an application-owned `TrustedReviewApproved` event through a command;
its reducer sets the field to true before the model request. The test rebuilds
the runtime and both dispatchers after every boundary, observes the true value
in the persisted model-request snapshot, and asserts exactly one privileged
tool invocation. The model and tool result cannot issue this approval event.

The happy-path external result contains prompt-injection text; output
validation removes the raw `content` field before the accepted result is
committed or sent to the next model turn (verified by both
`assertNotIn("content", ...)` on the persisted event and a full-history
substring check for the injected text). It also asserts that every persisted
model-request snapshot retains `trusted_evidence=false`, so raw retrieved
content does not grant trust in this workflow. The separate delete scenario supplies
its `ToolCall` directly from the test provider on the very first model turn,
before `search_invoices` (and therefore the injected document) is ever
fetched, so it does not demonstrate injected content steering a model into
proposing `delete_records`; it demonstrates only that the guard rejects that
tool name while `trusted_evidence` is `False`. These are representative
application-level scenarios, not a generic provenance guarantee or universal
prompt-injection protection.
