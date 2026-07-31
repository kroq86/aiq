# Durable model loop in FastAPI

This example is the 0.2 reference vertical:

```text
HTTP command → Ollama → Python tool → Ollama → answer → SSE/trace
```

It requires a local Ollama model and the FastAPI/Ollama extras:

```bash
pip install -e '.[fastapi,ollama]'
ollama pull llama3.2:1b
PYTHONPATH=src python examples/durable_model_loop_fastapi/main.py
```

Create a run and submit a command:

```bash
RUN_ID=$(curl -s -X POST http://127.0.0.1:8000/api/agents/assistant/runs | jq -r .run_id)
curl -X POST \
  http://127.0.0.1:8000/api/agents/assistant/runs/$RUN_ID/commands/message \
  -H 'content-type: application/json' \
  -d '{"text":"What is the weather in Tbilisi?"}'
curl -N http://127.0.0.1:8000/api/agents/assistant/runs/$RUN_ID/stream
```

Provider and tool objects are registration-specific operational resources.
The policy stores the selected tool definitions and continuation context in
events. If the process restarts, recreate the same versioned agent definition
and compatible resources against the same SQLite database.
