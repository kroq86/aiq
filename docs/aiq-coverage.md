# AIQ coverage contract: tickets 10–47

This document is the repository-local answer to the 38 AI-system questions
numbered 10 through 47. Coverage does not mean that Agentlog implements every
adjacent platform. Each ticket is classified as:

- `implemented` — the repository contains executable runtime behavior and a
  test or bounded formal check for the stated Agentlog capability;
- `partial` — Agentlog implements only its durable-runtime portion, exposes an
  application-owned seam, or provides scenario evidence rather than a generic
  subsystem;
- `out_of_scope` — the capability belongs to RAG, first-class MCP, broker, or
  production-operations infrastructure outside Agentlog's product boundary.

Evidence levels remain distinct: a passing scenario is not a universal proof,
a bounded model is not proof of Python, and a stable operation ID is not
exactly-once physical execution.

## Ticket 10: AI agent
- Status: `partial`
- Levels: `agent`
- Answer: Agentlog implements a bounded tool-using agent runtime in which a model proposes and deterministic code validates and executes; it is not a generic autonomous-agent taxonomy or planner.
- Evidence:
  - [Positioning](positioning.md)
  - [Bounded corporate example](../examples/bounded_corporate_agent/README.md)
- Boundary: The repository demonstrates the controlled tool-agent case, not every category from LLM through autonomous multi-agent systems.
- Not proved: Model autonomy, planning quality, and general task completion are not established.

## Ticket 11: Agent loop
- Status: `implemented`
- Levels: `agent`, `workflow`
- Answer: `DurableModelLoop` implements propose, persist, validate, execute, observe, continue, and terminal handling through reactions and durable effects.
- Evidence:
  - [Model-loop implementation](../src/agentlog/model_loop.py)
  - [Model-loop policy tests](../tests/test_model_loop_policy.py)
  - [Model-loop contract](model-loop.md)
- Boundary: The runtime controls execution; observation interpretation and next-action quality remain model/application responsibilities.
- Not proved: The loop does not prove that a proposed trajectory is useful or optimal.

## Ticket 12: Planning and execution roles
- Status: `partial`
- Levels: `agent`, `workflow`
- Answer: Agentlog separates model proposal, execution, and application validation, but it does not ship generic planner, router, coordinator, or verifier-agent components.
- Evidence:
  - [Validation contracts](../src/agentlog/validation.py)
  - [Validation is not planning](model-loop.md)
- Boundary: Planning strategy is application-owned; the core only provides a constrained execution seam.
- Not proved: Plan correctness, optimality, and multi-agent coordination are open.

## Ticket 13: ReAct
- Status: `out_of_scope`
- Levels: `agent`
- Answer: Agentlog can run an action/observation loop, but it does not implement the named Thought/Action/Observation ReAct protocol or store chain-of-thought.
- Evidence:
  - [Positioning](positioning.md)
  - [Model-loop contract](model-loop.md)
- Boundary: `@agent.react` means an event reaction and must not be confused with the ReAct prompting pattern.
- Not proved: Reasoning-trace correctness or ReAct-specific behavior is not claimed.

## Ticket 14: Deterministic workflow versus autonomous agent
- Status: `implemented`
- Levels: `agent`, `workflow`
- Answer: The model is allowed to propose one bounded tool action while code owns tool availability, validation, transitions, limits, and completion.
- Evidence:
  - [Durable model loop](../src/agentlog/model_loop.py)
  - [Bounded workflow E2E](../tests/test_v04_bounded_workflow_e2e.py)
  - [Bounded corporate example](../examples/bounded_corporate_agent/main.py)
- Boundary: This is a controlled middle ground, not an unrestricted autonomous agent.
- Not proved: The selected model action is not proved to be the best action.

## Ticket 15: Small models
- Status: `partial`
- Levels: `agent`
- Answer: Any `ModelProvider`, including the optional local Ollama provider, can be used; the bounded example shows a deterministic narrow proposer.
- Evidence:
  - [Ollama provider](../src/agentlog/providers/ollama.py)
  - [Bounded provider example](../examples/bounded_corporate_agent/main.py)
  - [Bounded Ollama integration evidence](../examples/bounded_corporate_agent/EVIDENCE.md)
- Boundary: Agentlog supplies the runtime contract, not classifier, reranker, extractor, or summarizer model training.
- Not proved: Reliability of a small model as planner or action selector is not established.

## Ticket 16: Structured output
- Status: `partial`
- Levels: `data`, `artifact`
- Answer: Tool arguments use a deliberately small JSON Schema subset, events enforce frozen JSON-compatible data, and application hooks perform semantic/business validation.
- Evidence:
  - [Tool schema validation](../src/agentlog/tools.py)
  - [Validation decisions](../src/agentlog/validation.py)
  - [Tool tests](../tests/test_tools.py)
- Boundary: Final model answers are strings; there is no generic Pydantic or full JSON Schema contract for arbitrary model decisions.
- Not proved: Structural validity does not establish semantic truth or business admissibility.

## Ticket 17: Tool calling
- Status: `implemented`
- Levels: `artifact`, `agent`
- Answer: A model proposes a typed `ToolCall`; the runtime persists the request, validates registry/schema/policy, executes the selected tool, and persists a distinct outcome.
- Evidence:
  - [Tool and model types](../src/agentlog/models.py)
  - [Tool lifecycle](../src/agentlog/model_loop.py)
  - [Tool tests](../tests/test_tools.py)
- Boundary: A model proposes the call but never owns physical execution or admissibility.
- Not proved: Tool usefulness and external-system correctness are application concerns.

## Ticket 18: Guardrails
- Status: `partial`
- Levels: `agent`, `workflow`
- Answer: Agentlog provides a tool allowlist, schema checks, application input/transition/output hooks, limits, workflow invariant, and goal gate.
- Evidence:
  - [Validation API](../src/agentlog/validation.py)
  - [Constrained execution E2E](../tests/test_v04_constrained_execution_e2e.py)
- Boundary: Human confirmation and generic role/permission policy are not built-in; domain guards are opt-in application predicates.
- Not proved: Configuring hooks does not prove that their policy is complete or correct.

## Ticket 19: State management
- Status: `partial`
- Levels: `artifact`, `agent`, `workflow`
- Answer: Canonical state is reconstructed by folding durable events; model-loop continuation can carry a validated workflow snapshot and state fingerprints across turns and restart.
- Evidence:
  - [Event core](../src/agentlog/core.py)
  - [Workflow snapshot implementation](../src/agentlog/model_loop.py)
  - [Snapshot/fingerprint restart-equivalence tests](../tests/test_v04_constrained_execution_e2e.py)
- Boundary: There is no universal schema for confirmed facts, candidates, pending actions, or trust markers.
- Not proved: Agentlog is not a general long-term memory system.

## Ticket 20: Basic RAG pipeline
- Status: `out_of_scope`
- Levels: `data`
- Answer: Agentlog does not implement parse, chunk, embed, or index stages.
- Evidence:
  - [Product boundary](positioning.md)
- Boundary: A RAG implementation may call Agentlog at its validated tool/effect boundary, but ingestion is a separate subsystem.
- Not proved: No retrieval quality or production RAG capability is claimed.

## Ticket 21: Embeddings
- Status: `out_of_scope`
- Levels: `data`
- Answer: The repository contains no embedding model, vector normalization, cosine/dot-product retrieval, or vector index.
- Evidence:
  - [Product boundary](positioning.md)
- Boundary: Vector representation and metric selection belong to an external retrieval system.
- Not proved: Semantic similarity quality is not measured.

## Ticket 22: Chunking
- Status: `out_of_scope`
- Levels: `data`
- Answer: Agentlog has no fixed, overlapping, semantic-boundary, or parent-child chunking implementation.
- Evidence:
  - [Product boundary](positioning.md)
- Boundary: Document preparation occurs before an observation reaches Agentlog's validation seam.
- Not proved: Chunk quality, recall, and context completeness are not evaluated.

## Ticket 23: Retrieval relevance threshold
- Status: `partial`
- Levels: `data`, `metrics`
- Answer: Application policy can reject or abstain based on a supplied score, but Agentlog does not retrieve candidates or calibrate a relevance threshold.
- Evidence:
  - [Bounded workflow relevance scenario](../tests/test_v04_bounded_workflow_e2e.py)
  - [Eval boundary](evals.md)
- Boundary: A hard-coded scenario threshold is not a calibrated retrieval component.
- Not proved: Precision/recall trade-offs and negative-query calibration are absent.

## Ticket 24: Metadata filtering
- Status: `partial`
- Levels: `data`
- Answer: Application guards demonstrate tenant, status, language, and score checks before accepting a result or transition.
- Evidence:
  - [Bounded workflow E2E](../tests/test_v04_bounded_workflow_e2e.py)
  - [Bounded corporate policy](../examples/bounded_corporate_agent/main.py)
- Boundary: These are application-owned checks after/proximate to a tool boundary, not a generic pre-retrieval hard-filter engine.
- Not proved: Permission and effective-date filtering across a retrieval index are not implemented.

## Ticket 25: Reranking
- Status: `out_of_scope`
- Levels: `data`
- Answer: Agentlog contains no bi-encoder, cross-encoder, LLM reranker, or reranking relevance gate.
- Evidence:
  - [Product boundary](positioning.md)
- Boundary: A reranker can be wrapped as a tool and validated, but its ranking semantics remain external.
- Not proved: Ranking quality and latency/cost trade-offs are not measured.

## Ticket 26: Hybrid search
- Status: `out_of_scope`
- Levels: `data`
- Answer: The repository does not implement BM25, vector search, RRF, score normalization, or hybrid retrieval.
- Evidence:
  - [Product boundary](positioning.md)
- Boundary: Agentlog can durably orchestrate an external search tool but is not the search engine.
- Not proved: Sparse/dense fusion quality is not established.

## Ticket 27: RAG evaluation
- Status: `partial`
- Levels: `data`, `metrics`
- Answer: Agentlog has a generic trace-scenario eval framework with comparison and restart evidence, but no retrieval or generation quality metrics.
- Evidence:
  - [Eval contract](evals.md)
  - [Eval implementation](../src/agentlog/evals/)
  - [Eval tests](../tests/test_evals.py)
- Boundary: Trace equality, terminal outcomes, and operation identity are not Recall@K, Precision@K, MRR, nDCG, groundedness, or citation accuracy.
- Not proved: RAG retrieval and answer quality are unmeasured.

## Ticket 28: Hallucination and groundedness
- Status: `partial`
- Levels: `data`, `artifact`, `metrics`
- Answer: Artifact references, causation metadata, and output normalization preserve selected provenance/evidence, but there is no claim-to-source citation model or groundedness scorer.
- Evidence:
  - [Artifact contract](artifacts.md)
  - [Instruction bindings](instructions.md)
  - [Bounded output normalization](../tests/test_v04_bounded_workflow_e2e.py)
- Boundary: Causal provenance of runtime events is different from semantic support for every claim in an answer.
- Not proved: Claim-level groundedness, correctness, and citation accuracy are absent.

## Ticket 29: Negative queries and abstention
- Status: `partial`
- Levels: `data`, `agent`, `metrics`
- Answer: `ValidationDecision("abstain")` produces a distinct durable `RunAbstained` terminal outcome when application policy finds insufficient evidence.
- Evidence:
  - [Validation decision](../src/agentlog/validation.py)
  - [Abstention scenarios](../tests/test_v04_constrained_execution_e2e.py)
  - [Run report](../src/agentlog/report.py)
- Boundary: Agentlog does not perform retrieval and does not calculate false-abstention or unsafe-answer rates.
- Not proved: Abstention quality on a labelled negative-query dataset is not established.

## Ticket 30: Prompt injection through retrieved data
- Status: `partial`
- Levels: `data`, `artifact`, `agent`
- Answer: The bounded example treats tool content as untrusted and commits only policy-selected normalized fields before the next model turn.
- Evidence:
  - [Bounded example limits](../examples/bounded_corporate_agent/README.md)
  - [Bounded workflow E2E](../tests/test_v04_bounded_workflow_e2e.py)
- Boundary: This is a deterministic normalization scenario, not a generic RAG prompt-injection defence or model sandbox.
- Not proved: Universal resistance to adversarial documents, tool descriptions, or model manipulation is not claimed.

## Ticket 31: Model Context Protocol
- Status: `partial`
- Levels: `artifact`
- Answer: The optional `MCPTool` adapter performs real official-SDK Streamable HTTP tool calls inside Agentlog's existing durable tool lifecycle.
- Evidence:
  - [Shipped MCP client adapter](../src/agentlog/mcp.py)
  - [MCP adapter contract](mcp.md)
  - [MCP adapter tests](../tests/test_mcp.py)
  - [Reference chat-to-MCP scenario](reference-chat-agent.md)
  - [MCP lifecycle boundary](positioning.md)
  - [Example MCP server](../examples/local_qaqc/mcp_server.py)
- Boundary: This is one statically configured client tool over Streamable HTTP, not an MCP server framework or complete MCP platform.
- Not proved: Complete protocol interoperability, all transports, discovery, and lifecycle conformance are not established.

## Ticket 32: MCP tools, resources, and prompts
- Status: `partial`
- Levels: `artifact`
- Answer: Statically declared tools can execute through the shipped MCP client; MCP resources and prompts are not first-class Agentlog artifacts.
- Evidence:
  - [Tool types](../src/agentlog/models.py)
  - [MCP adapter contract](mcp.md)
  - [Example MCP server](../examples/local_qaqc/mcp_server.py)
- Boundary: Agentlog's `ArtifactRef` and instruction templates are not claimed to implement MCP resources or prompts.
- Not proved: Dynamic tool discovery and transport of all three MCP artifact classes are absent.

## Ticket 33: MCP security
- Status: `partial`
- Levels: `agent`
- Answer: Application policy can constrain tool identity, arguments, transitions, and observations without delegating permissions to the model.
- Evidence:
  - [MCP boundary](positioning.md)
  - [MCP operation identity](mcp.md)
  - [Constrained execution E2E](../tests/test_v04_constrained_execution_e2e.py)
- Boundary: There is no core MCP trust store, server authentication policy, description sandbox, or generic permission framework.
- Not proved: Connecting an MCP server is not evidence that the server or its descriptions are trustworthy.

## Ticket 34: MCP versus REST
- Status: `out_of_scope`
- Levels: `artifact`
- Answer: Agentlog provides an optional FastAPI application boundary and a separate optional MCP Streamable HTTP tool adapter, but no unified REST/MCP abstraction.
- Evidence:
  - [FastAPI contract](fastapi.md)
  - [MCP adapter contract](mcp.md)
- Boundary: MCP discovery, resources/prompts, stdio, and protocol selection remain outside the core runtime.
- Not proved: REST/MCP semantic equivalence or protocol interoperability is not established.

## Ticket 35: MCP-result validation
- Status: `partial`
- Levels: `data`, `artifact`
- Answer: Generic input/transition/output hooks can structurally and semantically validate an external tool result and commit only a normalized application value.
- Evidence:
  - [Validation API](../src/agentlog/validation.py)
  - [Result validation path](../src/agentlog/model_loop.py)
  - [Real MCP lab evidence](../examples/local_qaqc/EVIDENCE.md)
  - [Constrained execution E2E](../tests/test_v04_constrained_execution_e2e.py)
- Boundary: The validator is application-owned and transport-neutral; there is no MCP-specific schema/tenant/language/freshness/provenance policy.
- Not proved: Arbitrary MCP observations are not automatically trustworthy.

## Ticket 36: State machine
- Status: `partial`
- Levels: `artifact`, `workflow`
- Answer: Runtime events, reducers, reactions, effects, and terminal registrations form an explicit event-driven state machine with durable reconstruction.
- Evidence:
  - [Runtime state machine](../src/agentlog/runtime.py)
  - [Formal model boundary](../formal/FORMAL_MODEL.md)
  - [Runtime tests](../tests/test_runtime.py)
- Boundary: The expanded v0.4 control-event vocabulary is scenario/restart tested but not included in one new saturated bounded proof.
- Not proved: The Python runtime universally refines every extended v0.4 transition.

## Ticket 37: Mathematical runtime model
- Status: `partial`
- Levels: `workflow`
- Answer: Tool hooks and reducers instantiate the sequence precondition/execute/observation-validation/transition/postcondition/invariant/goal for bounded model-loop scenarios.
- Evidence:
  - [Formal model](../formal/FORMAL_MODEL.md)
  - [Release evidence and bounds](release-evidence-0.4.md)
  - [Reference-model tests](../tests/model/)
- Boundary: These mechanisms are separate callbacks and local models, not one universal P→Execute→D→T→Q→I→G proof for arbitrary applications.
- Not proved: Expanded v0.4 control events do not have an exhaustive bounded proof as a single vocabulary.

## Ticket 38: Validation is not planning
- Status: `implemented`
- Levels: `agent`, `workflow`
- Answer: Validation accepts/rejects one proposed transition; it does not choose the next action or trajectory.
- Evidence:
  - [Validation is not planning contract](model-loop.md)
  - [Validation decision tests](../tests/test_v04_constrained_execution_e2e.py)
- Boundary: The planner remains a model/application responsibility.
- Not proved: Passing validation says nothing about plan optimality.

## Ticket 39: Preconditions and postconditions
- Status: `partial`
- Levels: `workflow`
- Answer: `validate_transition` checks an application precondition before tool execution and `validate_output` checks/normalizes the observed result afterward.
- Evidence:
  - [Execution policy](../src/agentlog/validation.py)
  - [Bounded workflow E2E](../tests/test_v04_bounded_workflow_e2e.py)
- Boundary: The hooks cover the tool-transition seam, not every arbitrary domain transition.
- Not proved: Application predicates are not automatically complete or formally correct.

## Ticket 40: Goal verification
- Status: `partial`
- Levels: `workflow`
- Answer: When configured, `goal_satisfied` is checked after the workflow invariant and gates `GoalSatisfied`, `AnswerProduced`, and `RunCompleted`.
- Evidence:
  - [Goal gate implementation](../src/agentlog/model_loop.py)
  - [Goal-gate scenarios](../tests/test_v04_constrained_execution_e2e.py)
  - [Completion-gate bounded model](../formal/completion_gate/README.md)
  - [Runtime mutants](../formal/model/verify_v04_runtime_mutants.py)
- Boundary: Workflows without a goal predicate retain the backward-compatible completion path; the configured goal is one application boolean predicate, and the bounded model is a local finite abstraction.
- Not proved: Goal truth for an arbitrary business domain, universal runtime refinement, and composition with the base lifecycle are not established.

## Ticket 41: Cycle detection and budgets
- Status: `partial`
- Levels: `agent`, `workflow`, `metrics`
- Answer: `ModelLoopLimits` bounds model steps, tool calls, and repeated state visits; terminal events make exhaustion observable and `RunReport` derives counts/flags. A standalone bounded model independently checks the guard's own safety properties with `WorkflowCycleDetected` non-vacuously reachable and two killed targeted mutants.
- Evidence:
  - [Limits and cycle guard](../src/agentlog/model_loop.py)
  - [Constrained execution tests](../tests/test_v04_constrained_execution_e2e.py)
  - [Run report tests](../tests/test_run_report.py)
  - [Cycle-guard bounded model](../formal/cycle_guard/README.md)
- Boundary: There are no token, monetary, wall-clock, or dynamic cost budgets in the model loop. The bounded model does not establish a refinement mapping from the real state-fingerprint mechanism to its abstract classes.
- Not proved: State-fingerprint repetition is an operational guard, not a proof that every semantic cycle is detected.

## Ticket 42: Retry, replan, abstain, and fail
- Status: `partial`
- Levels: `agent`, `workflow`
- Answer: `ValidationDecision` durably distinguishes accept, reject, retry, replan, abstain, and fail; abstain/fail have distinct terminal handling, while retry and replan currently share the same next-model-turn transition with different recorded status.
- Evidence:
  - [Decision type](../src/agentlog/validation.py)
  - [Decision-path tests](../tests/test_v04_constrained_execution_e2e.py)
- Boundary: The runtime preserves retry/replan intent as evidence but does not yet model them as different workflow states; neither means automatic safe repetition of a physical external effect.
- Not proved: Distinct status strings do not prove a different replanning strategy or an improved trajectory.

## Ticket 43: Async Python artifact boundaries
- Status: `partial`
- Levels: `artifact`
- Answer: Effects and dispatchers are asynchronous, reactions/reducers are synchronous and side-effect free, and persisted events define the serialization boundary.
- Evidence:
  - [Runtime effects](../src/agentlog/runtime.py)
  - [FastAPI lifecycle](fastapi.md)
  - [Lifecycle tests](../tests/test_fastapi_lifecycle.py)
- Boundary: Cancellation, per-effect timeout, and cross-process object transport are not a complete generic async framework.
- Not proved: Cancellation cannot guarantee rollback of an external effect already performed.

## Ticket 44: FastAPI and Pydantic
- Status: `implemented`
- Levels: `artifact`
- Answer: The optional FastAPI integration exposes typed request/response boundaries, lifecycle ownership, SSE replay, and a health endpoint with contract tests.
- Evidence:
  - [FastAPI integration](../src/agentlog/fastapi.py)
  - [Embedding contract tests](../tests/test_fastapi_embedding_contract.py)
  - [FastAPI documentation](fastapi.md)
- Boundary: FastAPI remains an optional adapter and does not redefine the event log as HTTP state.
- Not proved: A typed HTTP boundary alone does not provide production deployment or observability.

## Ticket 45: Idempotency and durable execution
- Status: `partial`
- Levels: `workflow`, `metrics`
- Answer: A durable request has a stable `operation_id`; dispatcher result/checkpoint commit is atomic and restart may retry the same physical effect with that identity.
- Evidence:
  - [Effect semantics](effects.md)
  - [Crash-window model](../formal/crash_window/)
  - [Crash-window equivalence tests](../tests/test_crash_window_equivalence.py)
- Boundary: Agentlog guarantees at-most-one committed result per request, not exactly-once physical execution or provider-side deduplication.
- Not proved: Duplicate attempts, dedup hits, replay counts, and effect retries are not production metrics.

## Ticket 46: Event sourcing and transactional outbox
- Status: `partial`
- Levels: `artifact`, `workflow`, `metrics`
- Answer: Immutable events are the source of truth and state is reconstructed by fold; subscription checkpoints support durable processing.
- Evidence:
  - [Event core](../src/agentlog/core.py)
  - [Formal event model](../formal/FORMAL_MODEL.md)
  - [SQLite event store tests](../tests/test_sqlite.py)
- Boundary: There is no transactional outbox message model, broker publisher, DLQ, or delivery-lag metrics.
- Not proved: Durable database append is not proof of delivery to an external broker.

## Ticket 47: Production deployment and observability
- Status: `partial`
- Levels: `metrics`
- Answer: Agentlog exports causal domain traces, replayable SSE, health state, eval summaries, and a derived run report.
- Evidence:
  - [Causal trace](../src/agentlog/trace.py)
  - [Flow X-Ray contract](flow-xray.md)
  - [Run report](../src/agentlog/report.py)
  - [Run report tests](../tests/test_run_report.py)
- Boundary: The package has no OpenTelemetry instrumentation, Prometheus exporter, SLOs, alerts, dashboards, HPA/deployment manifests, or rollout playbook.
- Not proved: Domain-event causality is not an end-to-end distributed production trace or quality-monitoring stack.

