# Packed trace-state dependency audit

This audit separates semantic state, derived data, and counterexample witness
data before defining the finite abstraction `beta`.

| Packed field | Read by `Next` | Read by invariant checker | Witness only | Derived | Current dedup key |
|---|---:|---:|---:|---:|---:|
| `history_len` | yes | yes | no | yes, from occupied event slots | yes |
| `reaction_cp` | yes | yes | no | no | yes |
| `effect_cp` | yes | yes | no | no | yes |
| `status` | yes | indirectly | no | yes, from terminal history | yes |
| `definition` | yes | yes | no | no | yes |
| `model_invocations` | written, not read | no | no | yes, from model results | yes |
| `tool_invocations` | written, not read | no | no | yes, from tool results | yes |
| `reserved/resource_match` | no | yes | no | no | yes |
| event `type` | yes | yes | no | no | yes |
| event `cause` | no | yes | no | no | yes |
| event `operation` | copied by `Next` | yes | no | partially | yes |
| event `flags` | yes | no | no | packed `model_step` and `tool_calls_used` | yes |

The BFS arrays `Parent`, `ParentAction`, `ViolationParent`, and
`ViolationAction` are witness metadata and are already outside the packed
state/dedup key.

## Findings

- Full history is required by the trace model because subscription dispatch
  reads `checkpoint + 1`, effects copy request identity, and invariants inspect
  causal predecessors. It must not be removed from this model.
- `status` is semantically redundant with terminal history but is read directly
  by `Next`; removing it requires changing the transition implementation and a
  preservation proof.
- Invocation counters do not affect enabled transitions or the existing eight
  invariants. They distinguish otherwise equivalent trace states only when
  result histories differ already, so they are candidates for removal from the
  semantic dedup key.
- Resource consistency is stored separately because the resource binding is an
  explicit input not derivable from event history.
- Exact positions in `cause` and `operation` are necessary for concrete trace
  witnesses. The abstract model may replace them only with finite relational
  classes such as `absent`, `matches`, and `mismatches`.
- The audit found that `ToolCallRequested` reset `flags` instead of carrying
  the loop class from `ModelCallSucceeded(tool)`. That made repeated tool loops
  unbounded and prevented `ModelLoopLimitExceeded`. The transition now carries
  the class forward; all proof artifacts must be regenerated before relying on
  the previous state/transition counts.
- A second audit found that the original FASM transition used that byte as one
  shared counter. The runtime and source specification have two independent
  counters and two guards. The low nibble now carries `model_step`; the high
  nibble carries `tool_calls_used`. Tool-call capacity is checked before
  `ToolCallRequested`; model-step capacity is checked before the next
  `ModelCallRequested` after a tool result.

No field is removed by this audit. Any dedup-key change requires transition and
property preservation checks first.
