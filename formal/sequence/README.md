# Sequential composition finite model

The model covers one fail-fast parent and three ordered children. Child state
is represented only by interface facts: stable run identity, status, accepted
output ownership, and terminal outcome count. Child histories are excluded.

The normal graph checks ordered execution, one active child, monotonic index,
immutable child identities, fail-fast parent behavior, output ownership, and
parent completion only after all children complete. A repeated parent start is
an explicit stuttering action and cannot allocate a second child identity. Ten
transition/interface mutants are checked by the unchanged invariant oracle.

This local model does not prove EventStore, child runtime implementations,
artifacts, concurrency, or composition refinement; those are separate
interface obligations and runtime scenarios.

Focused reproduction:

```bash
formal/sequence/verify
MODEL_MUTATION=duplicate_sequence_start formal/sequence/verify
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_sequence_runtime.SequenceRuntimeTests.test_duplicate_parent_start_does_not_allocate_another_child -v
```
