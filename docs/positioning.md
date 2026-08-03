# Positioning and competitors

Этот документ фиксирует конкурентную границу `agentlog`. Он не является
feature checklist или обещанием реализовать всё, что есть у других runtimes.

Последняя проверка официальных источников: 2026-07-28.

## Честная категория

`agentlog` находится в существующей категории durable execution. Сам принцип:

```text
durable history
→ recovery
→ external effects
→ continuation after crash
```

не является новым.

Продукт нельзя позиционировать как:

- первый durable agent runtime;
- первый agent runtime с replay;
- первый embedded durable workflow;
- первый runtime с fork;
- первый runtime, сохраняющий LLM/tool calls.

## Предлагаемая граница

> `agentlog` is an explicit event-sourced agent runtime for Python where the
> causal domain-event history is the public programming model, embedded in the
> application and usable without a separate server.

Короткая формулировка:

> Durable workflow systems persist execution. Agentlog makes the agent's causal
> domain history the product.

Это гипотеза позиционирования. Она станет реальным преимуществом только после
появления удобных trace, projection и fork APIs поверх domain events.

## Текущая метка зрелости

> **Single-worker durable guarded execution framework candidate.**

- Подходит для controlled single-worker pilot — один effect-воркер на run,
  без координации между несколькими воркерами.
- Внешние side effects требуют downstream idempotency на стороне самого
  tool/интеграции. At-most-one-committed-result внутри одного воркера —
  bounded/scenario-проверенное свойство (crash-window модель плюс
  восстановленные сценарии против реальных Ollama/MCP-крашей), а не
  exactly-once физическое исполнение.
- Multi-worker safety пока не заявляется.
- `EffectDispatchAttempt`/attempt ledger (`src/agentlog/attempts.py`) — это
  фундамент для будущего lease/claim protocol, а не сам lease protocol;
  несколько воркеров сейчас могут легитимно создать несколько физических
  попыток для одной операции.
- `RunAbstained` bounded model и attempt-telemetry сейчас в `## Unreleased` в
  `CHANGELOG.md`, а не в выпущенной `0.4.2`.
- Пять control event types (`GoalSatisfied`/`GoalNotSatisfied`/
  `WorkflowInvariantViolated`/`WorkflowCycleDetected`/`RunAbstained`) покрыты
  отдельными bounded-моделями с невакуозными witness'ами и targeted mutants;
  base trace/bisimulation reference-модель (`formal/model/spec.py`) для этих
  событий остаётся вакуозной, и composition между локальными моделями не
  установлена.

## Restate

[Restate Durable Agents](https://docs.restate.dev/ai/patterns/durable-agents)
описывает обычный handler, внутри которого выполняются LLM и tool calls.
Restate записывает шаги в journal, повторно использует завершённые результаты
при recovery и требует Restate Server перед agent services.

Основная модель:

```text
handler/service code
→ Restate journal
→ durable replay
```

Сильные стороны Restate:

- production runtime;
- durable LLM/tool calls;
- retries;
- observability;
- stateful services;
- pause/resume и orchestration patterns.

Потенциальная граница `agentlog`:

```text
Restate journal:
    runtime execution record

agentlog events:
    explicit public domain facts
```

Restate — самый прямой конкурент по durable-agent use case.

## DBOS

[DBOS architecture](https://docs.dbos.dev/architecture) строится вокруг
annotated workflows и steps:

```python
@DBOS.workflow()
def workflow():
    ...
```

DBOS library сохраняет workflow checkpoints и step outputs. Отдельный
orchestration server для локального приложения не обязателен.

Важная актуальная поправка:
[DBOS Python guide](https://docs.dbos.dev/python/programming-guide) указывает,
что SQLite используется по умолчанию, а PostgreSQL рекомендуется для production.
Поэтому embedded/local-first и SQLite сами по себе не отличают `agentlog` от
DBOS.

DBOS также поддерживает
[forking workflow from a step](https://docs.dbos.dev/python/tutorials/workflow-management).
Следовательно, fork сам по себе тоже не уникальная функция.

Основная граница:

```text
DBOS:
    durable Python functions and steps

agentlog:
    explicit immutable agent domain events and reducers
```

DBOS — наиболее опасный конкурент по lightweight Python durability.

## LangGraph

[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
сохраняет `StateSnapshot` на graph super-step boundaries. Основная abstraction:

```text
graph state
→ node
→ state update
→ checkpoint
```

LangGraph поддерживает:

- durable execution;
- SQLite/PostgreSQL checkpointers;
- state history;
- replay;
- time travel;
- fork from checkpoint;
- streaming;
- human-in-the-loop.

[Time travel documentation](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
явно описывает replay и fork. Поэтому эти слова нельзя использовать как
самостоятельную уникальность `agentlog`.

Различие:

```text
LangGraph source model:
    accumulated graph state and checkpoints

agentlog source model:
    immutable domain events; state is disposable projection
```

Это различие должно быть видно в пользовательском API, trace и debugging, иначе
оно останется implementation detail.

## Temporal

[Temporal Event History documentation](https://docs.temporal.io/workflow-execution/event)
описывает durable workflow execution через event history и deterministic replay.
Activities являются внешней effect boundary.

Основная модель:

```text
workflow code
→ execution history
→ deterministic replay
→ activities
```

Temporal — зрелый фундаментальный предшественник. Его преимущества:

- production durability;
- distributed execution;
- timers, retries and signals;
- mature operational tooling;
- broad language support.

Граница `agentlog`:

- agent-specific domain events;
- embedded single-process start;
- no separate Temporal service;
- causal history как public API, а не только execution history.

## Pydantic AI plus durable runtime

Конкурент — не только отдельный framework, но и комбинация:

```text
Pydantic AI
+
DBOS or Restate
```

Она даёт удобный typed agent API и готовую durability. `agentlog` не должен
соревноваться количеством LLM provider integrations.

Причина выбрать `agentlog` должна быть связана с event model:

- domain-level audit;
- causal lineage;
- deterministic state projection;
- agent-specific forensic trace;
- branch comparison на уровне domain facts;
- replayable UI projections из canonical log.

## Сравнение

| Project | Public programming model | Persistence model | Embedded start | Replay/fork |
|---|---|---|---:|---:|
| Restate | Services and handlers | Runtime journal | No, Restate Server | Replay |
| DBOS | Functions and steps | Workflow checkpoints | Yes, SQLite default | Replay and fork |
| LangGraph | Graph state and nodes | State checkpoints | Yes | Replay and fork |
| Temporal | Workflows and activities | Event History | No, service required | Replay |
| agentlog | Domain events, reducers, reactions, effects | Immutable event log | Yes, SQLite | State replay; fork not implemented |

## Что может стать отличием

### 1. Causal domain history

Планируемые first-class facts:

```text
event_id
correlation_id
causation_id
operation_id
stream_version
global_position
```

`event_id`, stream/global positions, explicit effect `operation_id` metadata и
versioned causal trace API реализованы. Cryptographic tamper evidence и runtime
call correlation пока отсутствуют.

### 2. Inspectable agent trace

Целевой API:

```text
UserMessageAdded
└── ModelCallRequested
    └── ModelCallSucceeded
        └── ToolCallRequested
            └── ToolCallSucceeded
                └── ModelCallRequested
                    └── AnswerProduced
```

Этот domain graph вычисляется `TraceService` из immutable stream. Python runtime
call graph остаётся отдельным слоем Flow Xray.

### 3. Domain-level fork

Fork должен оперировать agent events и явно определять:

- какие события наследуются;
- какие effects считаются уже выполненными;
- с какой точки разрешается новое выполнение;
- как связываются parent/fork streams;
- как сравниваются траектории.

Поскольку DBOS и LangGraph уже имеют fork, ценность должна быть именно в
domain-event semantics и causal comparison.

### 4. MCP lifecycle

Не просто MCP client:

```text
ToolCallRequested
→ stable operation_id
→ ToolCallSucceeded | ToolCallFailed | ToolCallRejected
```

`agentlog.MCPTool` реализует узкий настоящий client boundary через официальный
MCP Python SDK и Streamable HTTP. Он включается в обычный `ToolRegistry`, поэтому
существующая structural JSON Schema validation и опциональная
application-owned semantic policy выполняются до/после сетевого tool execution.
Retryable request rejection возвращается модели как feedback; postcondition
failure не повторяет внешний effect автоматически. Adapter не реализует MCP
server, discovery, resources/prompts, stdio, session pooling или trust policy;
точная граница описана в `docs/mcp.md`.

Эта policy является механизмом constrained execution, а не планировщиком:
она проверяет один предложенный переход, но не выбирает следующий допустимый
переход. Planning strategy остаётся контрактом конкретного приложения. v0.4
candidate добавляет opt-in ограниченный workflow-snapshot, repeated-state
guard и один boolean goal-предикат перед `RunCompleted` — это узкий,
scenario-tested механизм (см. `docs/release-evidence-0.4.md`), не общий
workflow-state model или production-grade goal verifier; ни один из них не
формально доказан (bounded exhaustive) на момент этого документа.

### 5. Replayable projections

SSE читается из immutable event log и восстанавливается через `Last-Event-ID`.
Это projection canonical history, а не эфемерный streaming bus.

## Решения по roadmap

Конкурентный parity не является основанием добавлять feature.

FastAPI command, replayable SSE и versioned causal trace export
(`schema_version=1`, `graph_kind=domain-event-history`) уже реализованы и
потребляются Flow Xray для завершённых runs. Следующий продуктовый slice:

```text
bidirectional domain-event/runtime-call IDs
→ causal event view correlated with actual Python runtime call subtree
```

Agentlog остаётся source of truth, Flow Xray — inspection/debugging UI. Отдельный
trace viewer внутри Agentlog не планируется. Граница текущего contract и
future runtime-call correlation описаны в `docs/flow-xray.md`.

Fork добавляется только после определения:

- parent/child stream data model;
- effect replay policy;
- branch comparison contract;
- acceptance scenario, который нельзя решить обычным новым run.

## One-line verdict

`agentlog` не выигрывает за счёт самой durability, SQLite или fork.

Он может выиграть, если immutable causal agent history станет настолько
удобным публичным API для audit, debugging, projections и branching, что
пользователь выберет events-first модель вместо workflow-first или state-first.
