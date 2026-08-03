# Changelog

Notable user-visible and verification changes are recorded here. Runtime
scenario evidence and bounded-model evidence remain separate from universal
correctness claims.

## Unreleased

No changes yet.

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

- Core-only installations can import `agentlog` without the MCP extra; MCP
  tests skip cleanly when the optional dependency is absent.
- The QA/QC image includes the package license during wheel metadata
  generation.

Source anchor:
[`6dcb02b` (MCPTool)](https://github.com/kroq86/agentlog/commit/6dcb02b).
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
[`73e13f9` (0.4.1 milestone)](https://github.com/kroq86/agentlog/commit/73e13f9)
and
[`a32ab20` (AIQ coverage follow-up)](https://github.com/kroq86/agentlog/commit/a32ab20).
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

Release tag: [`v0.4.0`](https://github.com/kroq86/agentlog/tree/v0.4.0).

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
[`17114e2`](https://github.com/kroq86/agentlog/commit/17114e2).
