# Trace evaluations

AIQ evals grade an existing durable `CausalTrace`. The eval package does
not execute providers or tools itself. A dataset names an async executor adapter
that runs one case through the application's normal durable path and returns the
exported trace.

```json
{
  "name": "tool-loop",
  "executor": "myapp.evals:execute_case",
  "cases": [
    {
      "id": "weather",
      "input": "Find the weather and save it",
      "expected_tools": ["get_weather", "save_result"],
      "expected_terminal": "completed",
      "max_model_steps": 3,
      "assertions": {
        "no_tool_failure": true,
        "stable_operation_ids": true
      }
    }
  ]
}
```

The executor uses the `module:callable` contract:

```python
async def execute_case(case: EvalCase) -> CausalTrace:
    ...
```

Run the dataset and optionally write a JSON report for CI:

```bash
aiq eval run dataset.json --json-report report.json
```

`--executor module:callable` overrides the dataset executor. The command exits
with `0` when every case passes, `1` when assertions fail, and `2` for invalid
input, configuration, or execution setup.

The version 2 report contains the dataset name, total/passed/failed counts,
each case's assertion failures, and a normalized durable trace summary. The
summary records terminal status, tool trajectory, model/tool-failure counts,
and digests for causal shape, operation-identity relations, and committed
observations. Concrete event IDs and timestamps are not compared.

Compare two version 2 reports without invoking a model or tool:

```bash
aiq eval compare baseline.json candidate.json \
  --json-report comparison.json
```

The command exits with `1` for a newly failing or missing baseline case, `0`
when there are no regressions, and `2` for invalid input. Non-regressive
behavior changes remain explicit as `changed`; they are not silently accepted
as equivalent.

## Restart equivalence

`RestartEquivalenceRunner` executes one normal trace and then asks an injected
`RestartableTraceExecutor` for supported persisted boundaries. Each restarted
execution must use a fresh runtime while retaining only state the adapter
declares durable. Results are `matched`, `mismatched`, or `unsupported`.

The comparison preserves terminal status, tool trajectory, committed
observations, causal shape, and operation-identity relations. It ignores
concrete event IDs and timestamps while preserving their relations. An empty
restart-point set is not a passing result.

This is scenario evidence only: it covers the cases and restart boundaries
actually executed. It is not universal refinement and does not establish
liveness or exactly-once physical execution.

For the declared model/tool crash window, `CrashWindowEvidence` keeps three
layers separate:

```text
durable CausalTrace
operational InvocationObservation log
effect checkpoint observed immediately after the crash
```

The fault-injection harness crashes after the adapter returns but before the
atomic result/checkpoint commit, recreates definition/resources, and retries
from the persisted request. The checked contract allows multiple physical
invocations, requires their `operation_id` to remain the request event ID, and
requires exactly one committed result with unchanged causation. Physical
invocations are never reconstructed or collapsed from event history.
