# Runtime refinement against the bounded formal model

This layer is separate from the FASM safety proof. Python extracts and
normalizes real Agentlog runtime snapshots but does not implement formal
transition rules. FASM emits the canonical 48-byte encoding for every formal
state; setdb remains the source of `Reachable` and `Transition`.

For each persisted runtime boundary the verifier checks:

```text
alpha(runtime snapshot) has an exact FASM StateEncoding
formal state is in setdb Reachable
successive formal state IDs are a setdb Transition pair
```

Run:

```bash
SETDB_BIN=/path/to/setdb PYTHONPATH=src:. \
  .venv/bin/python -m formal.refinement.verify_runtime
```

Covered scenarios are successful model/tool continuation with restart after
each dispatch, committed model failure, committed tool failure, forced
terminal handling, and a completed FastAPI command/SSE run. Runtime event UUIDs
are normalized to 1-based positions; causation and operation relations are
preserved. Atomic reaction outputs (`AnswerProduced`, `RunCompleted`) map to
formal sibling events with the same `ModelSucceeded` cause.

This does not yet refine crash-after-external-call-before-result-commit. The
current runtime snapshot exposes durable history/checkpoints, while physical
invocation count is operational and may increase without a durable change. A
separate instrumented operational snapshot is required to check that
at-least-once retry case without inventing data in the abstraction function.
