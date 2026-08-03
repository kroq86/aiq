# Local QA/QC lab evidence — 2026-08-01

## Protocol scope

Local integration of FastAPI, Agentlog `DurableModelLoop`, official MCP Python
SDK v1 Streamable HTTP, MinIO versioned objects, `SQLiteArtifactStore`, and
`SQLiteEventStore`. This is runtime scenario evidence, not formal proof or
universal implementation refinement.

## Executed scenarios

```text
deterministic happy path:
  RunCompleted; 4 tool requests; 4 tool successes; duplicate start HTTP 409

policy denial:
  RunFailed; 1 tool request; 1 durable tool outcome

latest object changed after pinning:
  RunCompleted using the exact previously returned MinIO version

digest mismatch:
  RunFailed before report creation

crash after PUT before external registration:
  process exit 86; fresh process completed; 1 committed save result;
  1 ToolCallRequested; 1 MinIO object version; 1 SQLite external identity

crash after external registration before result commit:
  process exit 86; SQLite external identity present while committed save result=0;
  fresh process completed with exactly 1 committed save result

timeout after successful PUT:
  MinIO object/version exists; SQLite external identity count=0; RunFailed

full process reopen:
  persisted completed history reopened as RunCompleted with 4 tool successes
```

## Packaged MCP adapter verification — 2026-08-03

The lab was rebuilt with `agentlog.MCPTool` from the installed package and MCP
Python SDK 1.29.0; the previous inline SDK transport was removed from
`app.py`.

Fresh deterministic HTTP acceptance:

```text
run: packaged-mcp-happy-20260803
terminal: RunCompleted
ToolCallRequested: 4
committed tool outcomes: 4
duplicate start: HTTP 409
```

Crash after the remote `save_report` invocation but before local artifact
registration:

```text
run: packaged-mcp-crash-20260803
process exit: 86
logical tool requests: 4
physical MCP CallTool requests: 5
committed save_report results: 1
terminal after restart: RunCompleted
operation_id: 2791a6dd-3847-4375-9800-f32672f7354d
```

The first three tools ran once and `save_report` ran twice across the crash.
FastMCP server logs supplied the physical request count. The single committed
`ToolCallSucceeded`, artifact `version`, and `created_causation` all carried the
same persisted `ToolCallRequested` operation identity. This is selected runtime
evidence for at-least-once execution and at-most-one committed result, not a
production retry metric or exactly-once physical execution claim.

## Ollama boundary

`OllamaProvider` reached the local `llama3.2:1b` service successfully. Two
planner attempts returned textual pseudo-calls in assistant content and zero
protocol-level `tool_calls`. The model therefore failed the four-tool product
acceptance. No normalizer or adapter converted that text into durable tool
events.

`qwen3:4b` was then downloaded and substituted as the only changed runtime
input. Prompt, tool schemas, parser, and acceptance normalizer were unchanged.
The model produced four protocol-level calls in order:

```text
list_rules -> stat_dataset -> run_qaqc -> save_report
```

All four tool results committed, the report used an exact
`s3://...versionId=...` reference, and the run ended in `RunCompleted`. The
unchanged acceptance process hit its 30-second polling limit after the first
tool result; the durable run continued and completed several minutes later.
Accordingly, that run passed protocol/trajectory acceptance while the
30-second latency gate failed on this machine. A single completed run did not
establish the separate 2/2 planner reliability criterion.

Per-call measurements for the completing run were:

```text
seconds:       22.474, 20.532, 89.294, 27.050, 58.340
input tokens:  357,    484,    638,    666,    924
output tokens: 675,    700,    3051,   971,    2013
```

A second run started while Qwen was already resident (`3.5 GB`, `100% GPU`,
context `4096`). It did not improve materially:

```text
seconds:       22.925, 85.542, 25.585, 27.945, 56.992
input tokens:  360,    487,    614,    768,    796
output tokens: 849,    2964,   924,    976,    2044
trajectory:    list_rules, list_rules, stat_dataset, run_qaqc
terminal:      RunFailed (maximum tool calls exceeded)
```

Cold loading is therefore not the dominant cost. Ollama documents thinking as
enabled by default for Qwen3 and accepts a top-level `think: false` API option.
The provider version used for these measurements did not send that option, so
the runs used Ollama's default thinking mode. Agentlog does not persist the
separate `message.thinking` response field, which means the durable
observations cannot split the reported output-token cost between thinking and
final content. Across two runs, protocol capability was demonstrated,
completion was 1/2, and repeatability remained open.

The provider was then extended with the compatible tri-state option
`think: bool | None = None`, and the unchanged lab configured `think=False`.
Two identical runs on Ollama 0.22.1 produced:

```text
run 1:
  total duration:   32.20 s
  model call:       31.988 s, input=357, output=992
  protocol calls:   list_rules, list_rules (same response)
  terminal:         RunFailed (multiple tool calls rejected)
  artifact identity: none

run 2:
  total duration:   39.44 s
  model call:       39.010 s, input=357, output=1364
  protocol calls:   list_rules, list_rules (same response)
  terminal:         RunFailed (multiple tool calls rejected)
  artifact identity: none
```

Both responses also contained long deliberation-like assistant content despite
the explicit API option. The provider payload mapping is unit-tested, but this
evidence does not establish how Ollama/Qwen internally applied the option.
Latency fell substantially relative to the earlier completing run, while the
required trajectory completed 0/2. Therefore `think=False` is an explicit lab
setting, not a recommended deterministic-planning mode for this model. The
deterministic provider remains the CI oracle and Qwen remains a separate
real-model acceptance boundary.

### Unchanged model-swap matrix

Three additional models were tested with only `OLLAMA_MODEL` changed. The
prompt, tool schemas, parser, single-tool policy, verifier, fault mode,
`think=False`, and 600-second timeout were unchanged.

```text
qwen2.5:3b, run 1 (7.48 s total):
  model calls: 3.471, 0.794, 0.815, 0.890, 0.892 s
  trajectory:  list_rules x5
  terminal:    RunFailed (maximum tool calls exceeded)

qwen2.5:3b, run 2 (4.61 s total):
  model calls: 0.536, 0.814, 0.873, 0.850, 0.865 s
  trajectory:  list_rules x5
  terminal:    RunFailed (maximum tool calls exceeded)

llama3.2:3b, run 1 (3.82 s total):
  model calls: 2.311, 1.205 s
  trajectory:  list_rules -> textual Python pseudo-call
  terminal:    RunCompleted; verifier rejected 1/1 tool boundaries

llama3.2:3b, run 2 (2.25 s total):
  model calls: 0.983, 0.797 s
  trajectory:  list_rules -> textual Python pseudo-call
  terminal:    RunCompleted; verifier rejected 1/1 tool boundaries

phi4-mini, run 1 (5.12 s total):
  model call:  4.860 s, input=291, output=56
  trajectory:  textual JSON-like pseudo-call
  terminal:    RunCompleted; verifier rejected 0/0 tool boundaries

phi4-mini, run 2 (1.21 s total):
  model call:  0.989 s, input=291, output=33
  trajectory:  textual JSON-like pseudo-call
  terminal:    RunCompleted; verifier rejected 0/0 tool boundaries
```

Each qwen2.5 response contained exactly one protocol call (`input` tokens
355/487/619/751/883 and `output` tokens 22 per step), but the planner did not
advance. The other two models emitted unsupported textual pseudo-calls, which
the runtime did not execute. All six runs produced no report artifact. The
required exact four-tool trajectory therefore passed 0/2 for every candidate.

References:

- <https://docs.ollama.com/capabilities/thinking>
- <https://ollama.com/blog/thinking>

## Release classification

The deterministic scenarios above are the lab's `0.3.0` release gate. Ollama
model runs are informational transport/capability acceptance and are not
release-blocking; no tested local model passed the required 2/2 planner
criterion, and real-model planner reliability remains open. The Compose images
and Python dependencies are not fully pinned, so this report records executed
scenario evidence rather than a byte-for-byte reproducible lab environment.

## Not proved or executed

- universal runtime refinement or liveness;
- real authentication/ACL/TLS behavior;
- concurrent runs under load;
- a passing real-LLM four-tool trajectory with the installed 1B model;
- cloud S3 behavior outside the local MinIO adapter;
- reliable real-model planning for any tested local model.

## Reproduction

```bash
docker compose -f examples/local_qaqc/docker-compose.yml up --build -d \
  minio minio-init mcp-server agentlog
python3 examples/local_qaqc/verify_acceptance.py
```
