# support_agent

The one reference app: a small but real support agent using only
Agentlog's public API (`agentlog.Agent`, `agentlog.CommandRejected`,
`agentlog.EffectFailed`, `agentlog.SQLiteEventStore`,
`agentlog.fastapi.AgentlogApplication`) — see `main.py` and the repo
README's "Public API" section.

This is the literal walkthrough for the project's own acceptance test:

> Install Agentlog, run the example, kill the process, start it again, see
> the run continue and its causal trace — without opening `runtime.py`.

## Run it

```bash
pip install "agentlog[fastapi]"   # or: pip install -e ".[fastapi]" from the repo root
python examples/support_agent/main.py support-agent.db
```

Leave it running in this terminal. It's a real `uvicorn` server on
`http://127.0.0.1:8000`, backed by a real SQLite file (`support-agent.db`)
in whatever directory you ran it from.

## Walkthrough (run these in a second terminal)

**1. Create a run** — this appends only `RunCreated`:

```bash
RUN_ID=$(curl -s -X POST http://127.0.0.1:8000/agents/support/runs | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "$RUN_ID"
```

**2. Send a message** — the only way a domain event enters the run:

```bash
curl -s -X POST "http://127.0.0.1:8000/agents/support/runs/$RUN_ID/commands/message" \
  -H "content-type: application/json" -d '{"text": "hello"}'
```

**3. Kill the server now** (`Ctrl+C` in the first terminal) — ideally right
after step 2, before the background effect has had a chance to run, so
what you're about to see is a genuinely *interrupted* run resuming, not
just a completed one being read again.

**4. Start it again**, same database file:

```bash
python examples/support_agent/main.py support-agent.db
```

**5. Read the state** — a fresh process, fresh `Agent`/`AgentlogApplication`
objects, same SQLite file. Nothing in this response depends on anything
that only existed in the killed process's memory:

```bash
curl -s "http://127.0.0.1:8000/agents/support/runs/$RUN_ID" | python3 -m json.tool
```

You should see `"answer": "echo: hello"` — the run continued and
completed under the new process.

**6. Read the causal trace**:

```bash
curl -s "http://127.0.0.1:8000/agents/support/runs/$RUN_ID/trace" | python3 -m json.tool
```

`terminal_status` is `"completed"`; `nodes` lists the full canonical
stream (`RunCreated → UserMessageAdded → ModelCallRequested →
ModelCallSucceeded → AnswerProduced → RunCompleted`); `edges` shows each
event's `causation_id` link back to the event that produced it.

## Other paths worth trying

**Rejected command** (never reaches the reducer, recorded as its own
domain event, not just an HTTP error):

```bash
RUN_ID2=$(curl -s -X POST http://127.0.0.1:8000/agents/support/runs | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
curl -s -X POST "http://127.0.0.1:8000/agents/support/runs/$RUN_ID2/commands/message" \
  -H "content-type: application/json" -d '{"text": ""}'
```

**Effect failure** (the fake model treats the literal text `fail` as an
outage -- produces `EffectFailed` then a terminal `RunFailed`, and does
*not* take down the rest of the server: `GET /agents/_health` stays
healthy):

```bash
RUN_ID3=$(curl -s -X POST http://127.0.0.1:8000/agents/support/runs | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
curl -s -X POST "http://127.0.0.1:8000/agents/support/runs/$RUN_ID3/commands/message" \
  -H "content-type: application/json" -d '{"text": "fail"}'
sleep 1
curl -s "http://127.0.0.1:8000/agents/support/runs/$RUN_ID3" | python3 -m json.tool
curl -s http://127.0.0.1:8000/agents/_health | python3 -m json.tool
```

## What this example deliberately does not show

- A real LLM (`FakeModel` in `main.py` is deterministic, no network, no
  API key needed) -- swapping it for a real one is just a different
  object passed as `context.model`.
- Multiple agent versions running side by side during a deploy -- see
  `docs/versioning.md` for that operational story; this example only
  declares `version="1"` once, running continuously.
