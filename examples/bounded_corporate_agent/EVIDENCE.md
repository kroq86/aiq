# Bounded Ollama integration evidence — 2026-08-03

## Protocol scope

This records one local execution pass through AIQ's real
`OllamaProvider`, bounded corporate workflow, and FastAPI adapter. It is
scenario evidence from one machine, not a model-quality guarantee, throughput
benchmark, or universal refinement proof.

Environment:

```text
macOS Darwin 24.6.0
Python 3.14.6
Ollama 0.22.1
```

## Fresh bounded runs

Each chat model received the unchanged `happy` scenario twice. Every attempt
used a new temporary SQLite database, so these were fresh runs rather than
resumes. `think=False` was unchanged.

```text
model               run 1                         run 2
phi4-mini:latest    RunFailed, 21.36 s            RunFailed, 3.01 s
llama3.2:1b         RunCompleted, 2.43 s           RunCompleted, 2.94 s
llama3.2:3b         RunCompleted, 5.35 s           RunCompleted, 2.44 s
qwen2.5:3b          RunFailed, 2.97 s              RunCompleted, 2.14 s
qwen3:4b            RunCompleted, 43.33 s          RunCompleted, 30.57 s
```

The result is model- and attempt-dependent. Three installed chat models passed
this one bounded prompt 2/2, Qwen 2.5 passed 1/2, and Phi-4 Mini passed 0/2.
That does not establish reliability on other prompts or workflows.

The sixth installed model, `nomic-embed-text:latest`, is an embedding model and
cannot participate in the chat/tool contract. A direct Ollama `/api/embed`
request succeeded with one 768-dimensional vector.

Model identities used:

```text
phi4-mini:latest           78fad5d182a7
llama3.2:3b                a80c4f17acd5
qwen2.5:3b                 357c53fb659c
qwen3:4b                   359d7dd4bcda
llama3.2:1b                baf6a787fdff
nomic-embed-text:latest    0a109f422b47
```

## Live FastAPI HTTP boundary

The reference FastAPI application was started against a fresh SQLite database
and `llama3.2:1b`. Real HTTP requests exercised health, create-run, command,
and trace endpoints.

```text
health: running, healthy=true
trace:
  RunCreated
  UserMessageAdded
  ModelCallRequested
  ModelCallSucceeded
  ToolCallRequested
  ToolCallRejected
  RunFailed
```

The model emitted argument keys `" Return"` and `" city"` instead of the
declared `city` key. AIQ rejected the call as `missing required arguments:
['city']`; the Python tool was not executed. This is a successful transport and
guard-boundary check, but not a successful model trajectory.

## Ollama unavailable

The full durable model loop was configured with an unreachable Ollama URL,
`http://127.0.0.1:9/api/chat`, and a 0.5-second timeout. This tested an actual
connection refusal without stopping the user's Ollama daemon:

```text
RunCreated
UserMessageAdded
ModelCallRequested
ModelCallFailed
RunFailed
```

This establishes the configured-endpoint connection-refusal path. It does not
exercise OS service restart or recovery of an in-flight request after the real
daemon is killed.

## Bounded concurrency

Four independent runs were created and commanded concurrently over HTTP
against one FastAPI application, one SQLite database, and `qwen2.5:3b`.

```text
concurrent-0  RunCompleted  10 events
concurrent-1  RunCompleted  10 events
concurrent-2  RunCompleted  10 events
concurrent-3  RunCompleted  10 events
client wall time: 8.26 s
worker health after completion: running, healthy=true
```

Every trace contained only its own `assistant:<run-id>` stream. This checks
bounded concurrent submission, stream isolation, and terminal completion. The
application may serialize effect processing; this is not evidence of four
simultaneous model executions, sustained-load capacity, an SLO, or a scaling
curve.

## Reproduction

Install the optional dependencies and ensure the listed models are pulled:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[fastapi,ollama]'
ollama list
```

Run a fresh bounded attempt by choosing a new database path:

```bash
PYTHONPATH=src:. .venv/bin/python \
  examples/bounded_corporate_agent/main.py \
  /tmp/aiq-fresh.sqlite happy --ollama --model llama3.2:1b
```

Start the HTTP example:

```bash
PYTHONPATH=src:. .venv/bin/python \
  examples/durable_model_loop_fastapi/main.py \
  /tmp/aiq-http.sqlite --model qwen2.5:3b --port 8766
```

The endpoint sequence is documented in
`examples/durable_model_loop_fastapi/README.md`. Delete or change the database
path between fresh runs; reusing it intentionally exercises durable resume.
