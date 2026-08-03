# Local QA/QC reference lab

This lab exercises the integration boundary, not a stronger formal claim:

```text
curl -> FastAPI/AIQ -> provider -> MCP Streamable HTTP -> MinIO
     -> SQLiteArtifactStore.register_external -> SQLite EventStore
```

The AIQ service composes the shipped `aiq.MCPTool` Streamable HTTP
client with QA/QC-specific artifact registration and fault injection. The MCP
server is a real official-SDK `FastMCP` process; its domain data remains a local
deterministic fixture.

The deterministic provider is the default because restart assertions must not
depend on planner sampling. `AIQ_PROVIDER=ollama` switches the same durable
loop to the real `OllamaProvider`.

## Start and run

```bash
cd examples/local_qaqc
docker compose up --build -d minio minio-init mcp-server aiq
curl -sS http://localhost:8000/health
curl -sS -X POST http://localhost:8000/runs \
  -H 'content-type: application/json' \
  -d '{"run_id":"acceptance-1"}'
curl -sS http://localhost:8000/runs/acceptance-1
python verify_acceptance.py
```

Wait until the history ends in `RunCompleted`. It must contain four
`ToolCallRequested`/terminal tool outcomes and one external report ref with an
`s3://...versionId=...` storage reference.

For Ollama, pull the model into the Compose service once and restart AIQ:

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull llama3.2:1b
AIQ_PROVIDER=ollama docker compose up --build -d aiq
```

`llama3.2:1b` is available as a transport smoke model, but in the recorded
2026-08-01 run it emitted textual pseudo-calls instead of Ollama `tool_calls`,
even after one prompt correction. Therefore it did not pass the four-tool
planner acceptance. Use a tool-capable model and rerun `verify_acceptance.py`;
do not parse textual pseudo-calls into durable tool events.

`qwen3:4b` passed the same protocol trajectory without prompt, schema, parser,
or normalizer changes:

```text
list_rules -> stat_dataset -> run_qaqc -> save_report -> RunCompleted
```

On the recorded M1/8 GB run, the unchanged 30-second acceptance poll timed out
after the first tool step, but the durable run continued and completed all four
protocol calls with an exact external report reference. That single run passed
protocol/trajectory acceptance and failed the 30-second latency gate; it did
not establish the separate 2/2 planner reliability criterion.

Keep the default 30-second gate for deterministic CI. Use a separate generous
wait for real local planning:

```bash
python verify_acceptance.py --timeout-seconds 600
```

One recorded Qwen run completed the exact four-tool trajectory; a second warm
run repeated `list_rules` and exhausted the four-tool limit. Protocol capability
is demonstrated, but planning repeatability is not established by those runs.

The lab sets `OllamaProvider(..., think=False)` explicitly. In two recorded
Ollama 0.22.1 runs, Qwen emitted two identical `list_rules` calls in its first
response. AIQ correctly rejected both responses under its single-tool
policy, so both runs ended in `RunFailed` before tool execution and produced no
artifact. Disabling thinking reduced the observed first-call duration to about
32 and 39 seconds, but did not make this model a reliable planner.

An unchanged two-run model-swap comparison also rejected every tested
candidate:

```text
qwen2.5:3b   list_rules x5 -> RunFailed, 0/2
llama3.2:3b  list_rules -> textual pseudo-call, 0/2
phi4-mini    textual pseudo-call only, 0/2
```

The deterministic provider remains the acceptance oracle. These local models
are transport/capability observations, not supported deterministic planners.
See `EVIDENCE.md` for per-call latency and token measurements.

## Release classification

```text
release gate:             deterministic provider scenarios
informational acceptance: local Ollama model observations
real-model 2/2 criterion: 0 models passed; not release-blocking
real-model planner reliability: open
```

The Compose dependencies currently use floating image tags and bounded Python
dependency ranges. The recorded runs are executed scenario evidence, not a
claim that the complete lab environment is byte-for-byte reproducible.

## Fault/policy modes

```bash
QA_POLICY=deny docker compose up --build -d mcp-server aiq
MCP_FAULT=change_after_pin docker compose up --build -d mcp-server aiq
MCP_FAULT=digest_mismatch docker compose up --build -d mcp-server aiq
MCP_FAULT=timeout_after_put docker compose up --build -d mcp-server aiq
AIQ_FAULT=after_put_before_registration \
  docker compose up --build -d aiq
AIQ_FAULT=after_registration_before_result \
  docker compose up --build -d aiq
docker compose restart aiq
```

Policy denial produces a durable tool failure without a report. The pinning
fault changes `latest` after `stat_dataset`; QA/QC must still read the exact old
version. Digest mismatch fails before report creation. Timeout-after-PUT leaves
an external object without a registered identity and commits a tool failure.

The two `AIQ_FAULT` modes terminate the AIQ process exactly once per
operation using a marker in the durable lab volume. Restart the service after
exit; the committed request is retried with the same operation identity,
physical execution may repeat, and the committed observation remains singular.

The acceptance script checks happy-path tool boundaries, exact external version
identity, terminal completion, and duplicate-start rejection. Object mutation,
digest mismatch, process-kill timing, and full reopen require explicit container
orchestration; they are not silently claimed by merely starting Compose.
