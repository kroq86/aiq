# Trace-model growth benchmark

`benchmark-bounds` runs the unchanged bounded FASM/setdb proof at several
history bounds and emits CSV. The default sequence is `4, 6, 8, 10, 12`.

```sh
formal/benchmark/benchmark-bounds 4 6 8 10
```

The benchmark measures the graph that the proof actually checks. It does not
turn bounded exploration into an unbounded proof and it does not validate an
abstraction.

The current FASM explorer deduplicates states with a linear scan. Consequently
the elapsed time includes both semantic graph growth and an `O(S^2 * state_size)`
implementation cost. State and transition counts are the primary complexity
evidence; timing is machine-dependent.
