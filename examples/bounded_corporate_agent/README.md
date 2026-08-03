# Bounded corporate agent

The one example that answers "what is Agentlog's v0.4 constrained-execution
API actually for": a bounded provider proposes at most one tool call, and
everything after that proposal is governed by durable, code-level policy —
not by asking a bigger model to behave.

```text
bounded provider -> validated tool proposal -> guarded transition
-> durable execution -> normalized result -> goal-gated completion
```

The default `FixedProposalProvider` is a deterministic lookup table, not an
LLM. The optional `--ollama` mode uses the real local `OllamaProvider` without
changing `BoundedCorporatePolicy` or `DurableModelLoop` wiring. Timestamped
local-model, FastAPI, failure-path, and bounded-concurrency results are recorded
in [EVIDENCE.md](EVIDENCE.md).

## Run it

```bash
PYTHONPATH=src:. python examples/bounded_corporate_agent/main.py demo.db happy --report
```

Real local model (requires `agentlog[ollama]`, a running Ollama daemon, and a
pulled model):

```bash
PYTHONPATH=src:. python examples/bounded_corporate_agent/main.py \
  demo.db happy --ollama --model qwen2.5:3b --report
```

Deterministic and Ollama runs use different stream/checkpoint identities, so
they can safely share one SQLite file. The Ollama mode is informational: model
tool-calling reliability is observed, not assumed, and a run may legitimately
end in `ModelOutputRejected`, a limit failure, or another guarded outcome.

Four scenarios, each a distinct guard in `BoundedCorporatePolicy`:

| Scenario | What it shows | Terminal event |
|---|---|---|
| `happy` | tenant guard passes, output is filtered/normalized (raw injected `content` field is stripped before commit), goal gate passes | `RunCompleted` |
| `wrong-tenant` | `validate_transition` rejects a proposal for a customer other than the allowed tenant, before the tool ever runs | `RunFailed` |
| `abstain` | `validate_output` finds no admissible document (wrong `status`) and abstains instead of answering from nothing | `RunAbstained` |
| `privileged-rejected` | `send_customer_email` is a privileged action; the transition guard rejects it unconditionally in this example (no trust signal is ever computed here — see the caveat below) | `RunFailed` |

## Durability: interrupt it and re-run the same command

```bash
PYTHONPATH=src:. python examples/bounded_corporate_agent/main.py demo.db happy
# Ctrl-C at any point, or just run the exact same command again:
PYTHONPATH=src:. python examples/bounded_corporate_agent/main.py demo.db happy
```

The second invocation with the same provider flags prints
`resuming an existing run from durable history`
and the causal event list is unchanged — the already-committed tool call is
not re-executed, and the run does not restart from the beginning. This is
ordinary Agentlog event-sourcing (`docs/model-loop.md`), not something
specific to this example.

## `--report`: a JSON run report

```bash
PYTHONPATH=src:. python examples/bounded_corporate_agent/main.py demo.db happy --report
```

Prints `agentlog.build_run_report`/`run_report_to_json` output: step counts,
tool outcome counts, validation retry counts, goal/invariant/cycle/abstain
control flags, and request->outcome latency in seconds. This is computed
fresh from the existing causal trace (`agentlog.TraceService`) — it adds no
new durable state and works for any `DurableModelLoop` agent, not just this
example. The example passes its own `loop.events` explicitly; this is required
when a loop uses a custom namespace, because event names are namespaced facts
and the report does not guess their semantics from suffixes. `tool_call`
latency covers the whole guarded effect (input/transition/output validation
plus the tool call itself, all committed in one batch) — it is not a separate
per-hook timing breakdown; see the module docstring in
`src/agentlog/report.py` for why.

## Honest scope

- `privileged-rejected` is an unconditional per-tool-name rejection in this
  example, not a real provenance/trust decision — there is no code path in
  this file that ever admits `send_customer_email`. Do not read it as
  evidence of a working trust gate; it shows *where* one would plug in, not
  that one exists. Same caveat as
  `docs/release-evidence-0.4.md`'s "Bounded corporate workflow gate"
  section, which this example mirrors.
- `FixedProposalProvider` always proposes the same call for a given
  scenario. It does not demonstrate a real model's action-selection
  reliability. `--ollama` crosses the real model transport boundary but is not
  a deterministic acceptance oracle; prior local-model evidence remains in
  `examples/local_qaqc/EVIDENCE.md`.
- Neither provider mode demonstrates resistance to prompt injection — the
  injected `content` field in `SCENARIO_DOCUMENTS` is stripped by
  `validate_output` regardless of what it says, and no scenario feeds fetched
  document content back into a proposal decision.
- This is a reference implementation, not a production MCP/RAG integration.
  Agentlog does not ship a real MCP adapter yet (`docs/positioning.md`
  Sec. "MCP lifecycle").
- The `score >= 0.78` relevance threshold in `validate_output` is an
  illustrative constant, not a calibrated value. A real deployment needs a
  labeled eval dataset and a precision/recall trade-off analysis before
  picking a threshold — this example does not do that and the number should
  not be copied as-is.
- `goal_satisfied` checks `state.accepted_invoice_count > 0`, read from the
  normalized tool result, specifically so it is not a restatement of "the
  tool call succeeded" (a tool can succeed with zero accepted documents;
  `validate_output` happens to route that case to `abstain` in this policy,
  so the two are not currently distinguishable by *this* example's four
  scenarios — the point is the goal predicate reads business content, not
  that this example proves the distinction observably).
