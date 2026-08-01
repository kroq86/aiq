# Agentlog 0.3 release evidence

Generated against the working tree on 2026-08-01. This report separates finite
model checking, runtime scenarios, and packaging evidence. It is not a claim of
universal implementation refinement.

## Public API and schema compatibility

- No name exported by `agentlog.__all__` in the `0.2.0` baseline was removed.
- Existing positional arguments of `ModelRequest` and `DurableModelLoop` retain
  their order; new arguments are appended with defaults.
- A serialized `0.2.0` `ModelRequest` without `artifacts` or `instruction` is
  accepted and defaults both fields correctly (`OLD_MODEL_REQUEST_READ_PASS`).
- `ModelRequest.to_data()` now always emits additive `artifacts` and
  `instruction` keys. This is backward-readable by the new runtime, but it is
  not byte-for-byte schema compatibility. An old runtime would ignore those
  semantics, so histories using them must remain definition-version isolated.
- `ModelLoopEvents` remains importable for compatibility, but its constructor is
  explicitly an unsupported internal implementation detail. The supported API
  is `DurableModelLoop.events`.
- `SQLiteArtifactStore.register_external(ref)` is new in `0.3.0`. The SQLite
  schema now distinguishes `embedded` and `external`; pre-external development
  databases are migrated as embedded at `open()`. `ArtifactStore.get()` returns
  bytes for embedded versions and the immutable `ArtifactRef` for external
  versions, because external blob retrieval belongs to its storage adapter.
- Package metadata is `0.3.0`; artifact hashes below refer to the final rebuild.

## Executable-model evidence

```text
Protocol scope:
  append/checkpoint core; abstract model/tool lifecycle; crash windows;
  middleware; exact artifacts; instruction resolution; linear fail-fast sequence

Formally established:
  Store finite local abstraction: inductive invariant checks pass.
  Crash-window finite counter abstraction: inductive invariant checks pass.
  Abstract lifecycle model: base and step checks pass.
  Concrete-to-abstract simulation: no unmatched initial states or transitions.
  Middleware/artifact/instruction/sequence: bounded finite checks pass.

Bounds/domain and state/transition counts:
  Store: 384 states, 8 transition relations.
  Crash window: 216 states, 16 transition relations.
  Abstract lifecycle: 54 states, 159 normalized transitions.
  Concrete simulation: 552 states, 1509 transitions, 33 projected transitions.
  Setdb saturation: bound 12 vs guard 14, 552 states, 1509 transitions.
  Middleware: bound 8, 14 states, 18 transitions.
  Artifacts: bound 7, 9801 states, 17115 transitions, 0 deadlocks.
  Instructions: domain 2x2x2, 13 checks.
  Sequence: 3 children, bound 12, 10 states, 16 transitions.

Non-vacuity and deadlocks:
  Initial and invariant states exist; normal completion is reachable.
  Terminal self-loops are intentional absorbing states. No unexplained deadlock
  was reported by the executed checkers.

Mutants killed/survived/equivalent/invalid:
  Abstract lifecycle: 5 killed / 0 survived / 0 equivalent / 0 invalid.
  Store: 8 killed / 0 survived / 0 equivalent / 0 invalid.
  Setdb core: 8 killed / 0 survived / 0 equivalent / 0 invalid.
  Crash finite model: 2 killed / 0 survived / 0 equivalent / 0 invalid.
  Middleware: 2 killed / 0 survived / 0 equivalent / 0 invalid.
  Artifacts: 7 killed / 0 survived / 0 equivalent / 0 invalid.
  Instructions: 5 killed / 0 survived / 0 equivalent / 0 invalid.
  Sequence: 10 killed / 0 survived / 0 equivalent / 0 invalid.

Runtime scenarios and unmatched boundaries:
  Store: 4 scenarios, 12 snapshots, 0 unmatched transitions.
  General refinement: 4 runtime scenarios + 1 FastAPI scenario, 122 snapshots,
  463/463 formal states reachable.
  Crash window: 1 instrumented model scenario, 5 snapshots, 2 physical
  invocations, 1 committed result, stable operation identity, 0 unmatched
  transitions. Python tests separately exercise model and tool crash windows.
  Restart persisted boundaries: 7 fresh-runtime boundaries exercised by suite.
  Sequence: success, fail-fast, and fresh-runtime stable child identity exercised.
  QA/QC MCP/Data-Lake simulation: 4 committed tool operations, exact dataset
  etag/digest and rules version, immutable report artifact, normal vs restart
  after every dispatcher boundary matched. The product-independent safety
  invariant oracle accepts both histories. UI transport, real MCP protocol,
  network/ACL behavior, and four-step planner transition refinement are not
  claimed by this scenario.
  QA/QC policy-denial scenario: access denial at dataset metadata is committed
  as `ToolCallRejected`, terminates as failed, invokes neither QA/QC nor report
  creation, creates no artifact, and matches after every-dispatch restart.
  QA/QC save-report crash window: 2 physical external-adapter invocations across
  2 runtime generations, stable operation identity, idempotent external
  registration, 1 committed result, 1 exact artifact version/digest, and 1
  `RunCompleted`. A separate scenario injects crash after external PUT but
  before SQLite registration and observes 2 physical PUTs with 1 registered
  identity. Crash evaluation scopes the committed result by request causation,
  so earlier tool results remain visible and cannot be collapsed by normalization.

Composition obligations open:
  Local models are connected by tested interface scenarios, not a universal
  composition proof. Sequence-to-child and Sequence-to-artifact obligations are
  scenario-checked only.

Liveness/fairness established or open:
  Open. Progress depends on dispatcher scheduling and eventual provider/tool/
  child-runtime response. No unconditional liveness claim is made.

Trusted computing base:
  CPython 3.14.6; FASM binary sha256
  b624f336360b492026163d81de004c401c5a79c71cb2222fb045b7350855d340;
  setdb binary sha256
  15d84e7163df26393cf3edfd201f41f756cc976284c9defb67766607c19c0f12;
  Python checker code, FASM encodings, runtime abstraction and normalization,
  unittest and Hypothesis.

Not proved:
  Universal runtime refinement; correctness of the trusted tools/encodings;
  unconditional liveness; exactly-once physical external invocation; arbitrary
  downstream compatibility with additive JSON keys; existence or byte identity
  of a real MinIO/S3 object. External object verification is an adapter obligation.
```

## Packaging and clean-environment evidence

Build command:

```bash
UV_CACHE_DIR=/private/tmp/agentlog-0.3-external-release/uv-cache \
  uv build --out-dir /private/tmp/agentlog-0.3-external-release/dist-a
```

Artifacts:

```text
wheel sha256 c6251e4d8a126c4116bafb1246abf96af79ba9c6b535e596c4044ea57a99e13e
sdist sha256 c3f47f1fddb780ef43219c9409e401c781573372ee631c45e5e532e4a4e65971
```

This evidence file is excluded from the sdist so recording the sdist digest
does not create an impossible self-referential hash.
Two independent builds were compared byte-for-byte and produced identical
wheel and sdist files (`REPRODUCIBLE_BUILD_PASS`).

The wheel was installed with `test`, `test-fastapi`, and `ollama` extras in a
new Python 3.14.6 venv. Import came from that venv's `site-packages`, the
`agentlog` console entry point displayed help, and a `ModelRequest` round trip
passed. The full installed-wheel suite completed:

```text
Ran 261 tests in 29.379s
OK (skipped=1)
```

The skip is `tests/model/test_setdb_bounded_model.py`, whose unittest discovery
requires `setdb` on `PATH`. The same backend was executed explicitly by the
formal verification commands above.

## Reproduction commands

```bash
formal/abstract/verify
formal/abstract/verify-mutants
formal/abstract/verify-simulation
formal/setdb/verify
formal/setdb/verify-mutants
formal/setdb/verify-saturation
formal/store/verify
formal/store/verify-mutants
formal/store/verify-runtime
formal/crash_window/verify
formal/crash_window/verify-mutants
formal/crash_window/verify-runtime
PYTHONPATH=src .venv/bin/python formal/middleware/check.py
PYTHONPATH=src .venv/bin/python formal/artifacts/check.py
for mutant in missing_version_invocation different_digest \
  different_storage_reference external_stores_blob \
  external_overwrites_embedded retry_second_logical_version \
  failed_registration_partial_row; do
  PYTHONPATH=src .venv/bin/python formal/artifacts/check.py --mutant "$mutant"
done
PYTHONPATH=src .venv/bin/python formal/instructions/check.py
PYTHONPATH=src .venv/bin/python formal/sequence/check.py
PYTHONPATH=src .venv/bin/python -m unittest tests.test_qaqc_e2e_model -v
FASM_BIN=/opt/homebrew/bin/fasm SETDB_BIN=/tmp/agentlog-setdb-bin \
  PYTHONPATH=src:. .venv/bin/python -m formal.refinement.verify_runtime
python -m unittest discover -s tests -q
```

## Remaining release gates

- Repository-wide Ruff is not green: 13 pre-existing findings were observed,
  including undefined names in dormant test paths. The changed Sequence and
  finite-model files pass focused Ruff checks, but lint must not be reported as
  globally passing.
