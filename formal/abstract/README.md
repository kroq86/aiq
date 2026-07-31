# Parameterized ModelLoopModel

This directory contains the finite FASM/setdb over-approximation of independent
positive model-step and tool-call limits. Each counter uses `low`, `before`, and
`at` classes. `low -> low | before` is nondeterministic, so the model admits at
least every concrete finite-limit behavior; it is not an exact quotient.

Run the diagnostic boundaries first:

```sh
./formal/abstract/verify-generate
./formal/abstract/verify-emit
```

Expected:

```text
GENERATE_OK states=54 transitions=219
EMIT_OK facts=4 bytes=104
```

Run the complete graph and inductive checks:

```sh
./formal/abstract/verify
```

Expected:

```text
ABSTRACT_GRAPH_PASS states=54 raw_transitions=219 transitions=159
BASE_PASS violations=0
STEP_PASS violations=0
ABSTRACT_PASS states=54 inv=49 transitions=159 bad_transitions=0
```

Run limit mutation sensitivity and the fixed `2/2` trace simulation:

```sh
./formal/abstract/verify-mutants
./formal/abstract/verify-simulation
```

Expected final results:

```text
ABSTRACT_MUTATION_MATRIX_PASS mutants=5
SIMULATION_PASS concrete_states=552 concrete_transitions=1509 projected_transitions=33 unmatched_initial=0 unmatched_transitions=0 concrete_bad=0
```

`concrete_bad=0` is the normal saturated trace graph and therefore makes the
bad-state preservation check vacuous in this command. The existing eight trace
mutants independently establish predicate sensitivity; projecting those
mutant graphs is a separate strengthening, not part of the current simulation
claim.

The fact writer issues one syscall per short fact. Because the local FASM Mach
wrapper does not provide a reliable reusable raw-syscall status in this call
path, completeness is checked externally by exact fixture diff, fact counts,
and successful setdb loading.
