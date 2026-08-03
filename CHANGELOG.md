# Changelog

Notable user-visible and verification changes are recorded here. Runtime
scenario evidence and bounded-model evidence remain separate from universal
correctness claims.

## Unreleased

## 0.5.1 - 2026-08-03

### Added

- GitHub Actions build, metadata validation, and token-authenticated PyPI
  publishing for version tags and explicit manual dispatches.
- English PyPI project page with searchable package metadata, project URLs,
  installation guidance, capability boundaries, and verification links.

### Changed

- Reworked the GitHub landing section and repository metadata around AIQ's
  durable agent runtime, event sourcing, guarded tools, replay, MCP/FastAPI
  adapters, and SQLite lease/fencing capabilities.

## 0.5.0 - 2026-08-03

### Added

- Opt-in same-file SQLite effect leases with atomic claim/attempt recording,
  full-stream terminal/committed admission, fresh lease IDs, pre-handler
  ownership confirmation, DB-time heartbeat renewal, monotonic fencing,
  takeover after expiry, and atomic stale-worker rejection at commit.
- Append-only durable lease observations for acquired, busy, expiry, renewal,
  takeover, stale ownership, and stale commit facts.
- Standalone lease/fencing bounded model covering 20,361 states within
  transition bound 8, six bounded semantic mutants, 12 killed runtime source
  mutants with verified source restoration, eight controlled refinement
  scenarios, and bounded SQLite stress/soak tests.

### Changed

- Only `EffectLeaseOptions` is exported as public lease API; lease handles,
  protocols, errors, claim outcomes, and observation records remain internal.

### Boundaries

- Lease mode establishes one current DB-valid owner and prevents stale durable
  commits. It does not guarantee exactly-once physical execution, prevent
  overlap after expiry, coordinate different databases, or remove the need for
  downstream idempotency.

## 0.4.3 - 2026-08-03

### Added

- Standalone RunAbstained bounded model covering request/result
  validation-failure routing in 8 reachable states, with five targeted mutants
  killed.
- Opt-in append-only effect-dispatch attempt ledger with fail-closed recording,
  stable operation-ID correlation, and explicitly supplied RunReport
  aggregates.

### Changed

- The supported deployment contract now explicitly requires one active effect
  worker per canonical subscription unless an external lease/fencing protocol
  provides single-flight coordination.
- Crash-window runtime refinement now correlates controlled provider entries
  with the real durable attempt ledger.
- Contribution guidance now requires narrow executable contracts, mutation
  sensitivity, runtime scenarios, and explicit unproved boundaries for
  safety-critical transitions.

### Boundaries

- This release does not provide multi-worker ownership, lease/fencing,
  transactional outbox delivery, exact external-call counts, provider-side
  deduplication, universal runtime refinement, or composition of local models.

## 0.4.2 - 2026-08-03

### Added

- Optional `MCPTool` client adapter using the official MCP Python SDK over
  Streamable HTTP, with static tool definitions, bounded sessions, JSON result
  normalization, and optional protected operation-ID injection.
- Real FastMCP/MinIO happy-path and crash/restart evidence through the packaged
  adapter.
- Standalone CompletionGateModel covering independent invariant/goal
  configuration axes in 15 reachable states and 11 transitions, with all five
  targeted mutants killed.

### Changed

- AIQ coverage and formal-model documentation now distinguish local
  bounded-model evidence from runtime refinement, composition, and business
  predicate correctness.
- The local QA/QC lab now composes the packaged MCP transport instead of
  maintaining a duplicate inline client.

### Fixed

- Core-only installations can import `aiq` without the MCP extra; MCP
  tests skip cleanly when the optional dependency is absent.
- The QA/QC image includes the package license during wheel metadata
  generation.

Source anchor:
[`6dcb02b` (MCPTool)](https://github.com/kroq86/aiq/commit/6dcb02b).
CompletionGateModel is part of the working tree prepared for this release.

## 0.4.1 - 2026-08-03

### Added

- Bounded corporate-agent reference example with deterministic and real local
  Ollama modes plus timestamped multi-model evidence.
- Derived `RunReport` projection for committed model/tool counts, validation
  outcomes, goal/control flags, and causal latency.
- Standalone CycleGuardModel with `WorkflowCycleDetected` reachable and two
  targeted mutants killed.
- Machine-checked AIQ coverage contract for tickets 10 through 47.

### Changed

- Custom model-loop namespaces are explicit inputs to run-report generation.
- Lint and restart tests were stabilized without changing runtime semantics.

Repository anchors:
[`73e13f9` (0.4.1 milestone)](https://github.com/kroq86/aiq/commit/73e13f9)
and
[`a32ab20` (AIQ coverage follow-up)](https://github.com/kroq86/aiq/commit/a32ab20).
No `v0.4.1` tag was created and package metadata remained `0.4.0`.

## 0.4.0 - 2026-08-03

### Added

- `ExecutionPolicy` input, transition, pre-state, and output validation seams
  with explicit accept/reject/retry/replan/abstain/fail decisions.
- Optional normalized workflow snapshots, invariant and goal gates,
  repeated-state detection, and bounded model/tool limits.
- Constrained-execution end-to-end, restart-equivalence, and targeted runtime
  mutation scenarios.

### Boundaries

- No exactly-once physical execution, general planner, production RAG system,
  or complete proof for the expanded control-event vocabulary was claimed.

Release tag: [`v0.4.0`](https://github.com/kroq86/aiq/tree/v0.4.0).

## 0.3.0 - 2026-08-01

### Added

- Exact embedded/external artifact identities and SQLite artifact storage.
- Instruction resolution, middleware, linear sequence composition, and
  scenario/restart evaluation APIs.
- Crash-window, QA/QC, and restart harnesses.
- Local bounded models for artifacts, instructions, middleware, sequences,
  event storage, and lifecycle abstractions.

### Boundaries

- Evidence remained finite/bounded or scenario-level; arbitrary application
  definitions and external systems were not universally proved.

Release commit:
[`17114e2`](https://github.com/kroq86/aiq/commit/17114e2).
