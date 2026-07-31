# ollama_chat_agent

Same declarative Agent API as `examples/support_agent`, but the effect
handler makes a real HTTP call to a local [Ollama](https://ollama.com)
instance instead of a deterministic fake. This is what proves (or would
disprove) that the durable-effect contract survives a genuinely slow,
non-deterministic external call, not just an instant fake one.

## Setup

```bash
ollama pull llama3.2:1b   # ~1.3GB; safe on constrained RAM (e.g. 8GB M1) --
                          # do not use a larger model on such a machine
curl http://127.0.0.1:11434/api/tags   # confirm Ollama is up and the model is listed
```

## Run the app

```bash
PYTHONPATH=src python examples/ollama_chat_agent/main.py ollama-chat.db --model llama3.2:1b
```

Then drive it exactly like `examples/support_agent` (`POST .../runs`,
`POST .../commands/message`, `GET .../runs/{run_id}`, `GET .../trace`) --
see that example's README for the exact `curl` walkthrough.

## What was verified against a real local Ollama (`llama3.2:1b`, macOS, 8GB M1)

All five scenarios below were run for real, not simulated by inspection --
including two that inject a controlled fault (a "crash" and a race window)
around **genuine** Ollama HTTP calls, since a live `kill -9` cannot be
timed precisely enough to land inside a millisecond-scale commit window.

1. **Real happy path** -- `python examples/ollama_chat_agent/main.py`, real
   `uvicorn`, real `curl`: `RunCreated → UserMessageAdded →
   ModelCallRequested → ModelCallSucceeded → RunCompleted`. Confirmed: the
   answer in `state` is Ollama's real response, `GET .../trace` shows the
   full causal chain via `causation_id`.

2. **Restart after Ollama already answered** -- sent a message, waited for
   completion, killed the server, restarted it against the same SQLite
   file. State was identical; the run is terminal, so no dispatcher
   attempts it again -- Ollama structurally cannot be re-invoked for an
   already-completed run.

3. **Ollama down** -- stopped the real `ollama serve` process, sent a
   message: `ModelCallRequested → EffectFailed → RunFailed`
   (`failure_reason: "Ollama request failed: All connection attempts
   failed"`). `/agents/_health` stayed healthy throughout; a separate,
   already-completed run was unaffected.

4. **Crash after the real external call, before commit**
   (`verify_crash_before_commit.py`) -- a store wrapper raises on the
   first commit carrying real output, simulating the process dying right
   after Ollama responds. Confirmed via an HTTP-call counter: Ollama was
   invoked **twice** (once before the simulated crash, once after a fresh
   "restart" generation retries the still-pending effect); exactly **one**
   `ModelCallSucceeded` was ever durably committed, and its `operation_id`
   matches the stable `ModelCallRequested` event's `event_id` across the
   crash/retry boundary. This is the honest boundary documented in
   `docs/effects.md`: commit retry ≠ effect retry, and a crash in exactly
   this window can cause a real duplicate external call.

5. **Terminal race during a real, slow effect** (`verify_terminal_race.py`)
   -- Ollama's real response is artificially held for 2s after it
   actually arrives (to make the race window reliable), while a
   concurrent task forces `RunCompleted` onto the same stream through a
   different path. Confirmed: the late, real `ModelCallSucceeded` is
   discarded, not committed after `RunCompleted` -- terminal absorption
   holds for a genuinely slow real external call, not just a fake instant
   one, and the effect subscription's checkpoint still advances (no
   stuck dispatcher).

Scripts 4 and 5 are manual integration checks, not part of `tests/` --
they need a running local Ollama and are not expected to run in an
environment without one.
