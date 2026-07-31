# Agentlog + Flow Xray

## Граница ответственности

```text
Agentlog   = canonical immutable causal domain history
Flow Xray  = external visualization consumer of that history
```

Agentlog:

- владеет canonical durable domain-event history одного run;
- владеет causal metadata (`causation_id`, `correlation_id`, `operation_id`);
- экспортирует её как versioned plain JSON (`schema_version=1`,
  `graph_kind=domain-event-history`) — не рендерит graph/timeline сам и не
  хранит UI-специфичное состояние.

Flow Xray:

- потребляет domain-event-history JSON;
- рендерит его как отдельный domain graph;
- не превращает эти nodes в `TraceNode.children` — там parent/child означает
  Python runtime call, а не domain causation, и смешивать эти две модели
  нельзя;
- остаётся независимо устанавливаемым пакетом: Agentlog не импортирует Flow
  Xray и не зависит от его схемы, contract — только plain JSON.

Completed Agentlog run уже успешно потребляется и рендерится Flow Xray через
этот JSON — это не только направление, а подтверждённое текущее состояние.

## Что реализовано: causal trace export

`src/agentlog/trace.py` предоставляет чистую, вычисляемую заново каждый раз
проекцию immutable event log одного run — `CausalTrace`. Это не второй event
store и не mutable trace table: экспорт всегда строится из
`store.load(stream_id)` в момент запроса.

Public API:

```python
from agentlog import TraceService

service = TraceService(store=store, agents={"energy-assistant": agent})
trace = await service.export("energy-assistant", run_id)
```

`CausalTrace` содержит: `agent_name`, `run_id`, canonical-ordered `events`
(каждый — `TraceEvent` с `event_id`, `event_type`, `stream_id`,
`stream_version`, `global_position`, `correlation_id`, `causation_id`,
`operation_id`, `data`, `metadata`, `created_at`), `edges` (построены строго
из `metadata.causation_id`, не из соседства в потоке; на wire — `source_event_id
-> target_event_id`, см. точную схему ниже), `roots` (события без
`causation_id`), `dangling_causation` (события, чей `causation_id` не
разрешился ни к одному событию в этом run — сохраняются, а не отбрасываются),
`terminal`/`terminal_event_type` (Python-уровень) и `latest_stream_version`.
Internal Python field names (`CausalEdge.cause_event_id`/`effect_event_id`) не
обязаны совпадать с именами полей в сериализованном JSON — единственный
контракт для внешнего consumer'а это `trace_to_json()`, схема ниже.

Неизвестный run или run, запрошенный под чужим `agent_name`, вызывают
`RunNotFoundError` — API не различает "нет такого run" и "run принадлежит
другому агенту".

HTTP-граница (опционально, если приложение уже использует `agentlog.http`):

```http
GET /agents/{agent_name}/runs/{run_id}/trace
```

Возвращает тот же `CausalTrace` в виде JSON (`trace_to_json`). 404 для
неизвестного run, неизвестного agent_name и run под чужим agent_name —
идентично остальным endpoint'ам этого adapter'а.

### Demo command (subprocess boundary for Flow Xray)

Не требует HTTP, не требует `PYTHONPATH`, не требует запуска из этого
репозитория — работает из установленного пакета:

```bash
python -m agentlog.demo --status completed --output trace.json
python -m agentlog.demo --status active    --output trace.json
```

Генерирует ровно один JSON-документ через `TraceService`/`trace_to_json`
(реальный exporter, не hand-built JSON), приводя fake-adapter reference
agent к `terminal_status=completed` (9 nodes / 8 edges) или
`terminal_status=active` (4 nodes, обрывается сразу после committed
`ToolCallRequested` с persisted `operation_id`, до его effect). No real
LLM/MCP, no network, no Flow Xray import. Reusable logic живёт в
`agentlog.demo` (`generate_completed_trace`, `generate_active_trace`,
`write_trace_json`); `examples/export_flow_xray_traces.py` — тонкая обёртка
над тем же кодом для генерации обоих файлов в одну директорию.

Совмещение этой команды с HTML-рендерингом в один one-command demo —
ответственность Flow Xray, не Agentlog: эта команда производит только
Agentlog JSON.

### Что означает этот контракт

- Экспортируемый уровень — **domain event**, не Python function call.
  `TraceEvent` — это то, что уже сохранено в event log (`ModelCallRequested`,
  `ToolCallSucceeded`, ...), а не стек вызовов рантайма, который их произвёл.
- **Runtime call correlation — следующее расширение, не часть этого контракта.**
  Если Flow Xray умеет сопоставлять domain event с конкретным runtime call
  subtree (см. предложенный мост ниже), это отдельный, более поздний шаг,
  требующий проверки реальной схемы Flow Xray.
- **Trace — inspectable history, не cryptographic tamper evidence.** Экспорт
  доказывает то, что реально сохранено в SQLite (append-only на уровне схемы,
  see `docs/effects.md`), но не является подписанным/hash-chained proof
  неизменности. Владелец файла базы физически может её заменить.
- **Fork не подразумевается.** `CausalTrace` — read-only проекция одного
  существующего run. Экспорт не создаёт новый stream и не даёт guarantee о
  возможности продолжить/разветвить run из любой точки; fork остаётся
  отдельным, нереализованным контрактом (`docs/positioning.md`).
- **`operation_id` читается, а не изобретается.** Effect request events несут
  explicit `metadata.operation_id` (обычно равный `event_id` самого request
  event); result events несут тот же `operation_id` и `causation_id`,
  указывающий на request. `trace_to_json()` просто surface'ит то, что уже
  persisted в metadata — не вычисляет и не подставляет значение. Полный
  effect identity/at-least-once контракт: `docs/effects.md`. Agentlog не
  обещает exactly-once внешнее исполнение: если процесс падает после внешнего
  side effect, но до SQLite commit, операция может повториться с тем же
  `operation_id`.

## Agentlog trace contract

```http
GET /agents/{agent_name}/runs/{run_id}/trace
```

Возвращает plain JSON. Это единственный актуальный пример схемы v1 в этом
документе — сокращённый (2 из 10 nodes показаны полностью), но структурно
точный вывод для реального reference HTTP run
(`examples/http_chat_agent/main.py`, `POST /agents/energy-assistant/runs`):

```json
{
  "schema_version": 1,
  "graph_kind": "domain-event-history",
  "agent_name": "energy-assistant",
  "run_id": "run-123",
  "terminal_status": "completed",
  "latest_stream_version": 9,
  "roots": ["event-0"],
  "nodes": [
    {
      "event_id": "event-0",
      "event_type": "RunCreated",
      "stream_id": "energy-assistant:run-123",
      "stream_version": 0,
      "global_position": 41,
      "correlation_id": "run-123",
      "causation_id": null,
      "operation_id": null,
      "data": {"agent": "energy-assistant"},
      "metadata": {"correlation_id": "run-123"},
      "created_at": "2026-07-28T15:00:34.487381+00:00"
    },
    {
      "event_id": "event-1",
      "event_type": "UserMessageAdded",
      "stream_id": "energy-assistant:run-123",
      "stream_version": 1,
      "global_position": 42,
      "correlation_id": "run-123",
      "causation_id": "event-0",
      "operation_id": null,
      "data": {"text": "Pressure for A-17"},
      "metadata": {"correlation_id": "run-123", "causation_id": "event-0"},
      "created_at": "2026-07-28T15:00:34.487381+00:00"
    }
  ],
  "edges": [
    {"source_event_id": "event-0", "target_event_id": "event-1", "kind": "caused"}
  ],
  "timeline": [
    {"event_id": "event-0", "stream_version": 0, "global_position": 41},
    {"event_id": "event-1", "stream_version": 1, "global_position": 42}
  ],
  "dangling_causation": []
}
```

Полный run — 10 nodes / 9 edges, canonical event sequence:

```text
RunCreated
UserMessageAdded
ModelCallRequested
ModelCallSucceeded
ToolCallRequested
ToolCallSucceeded
ModelCallRequested
ModelCallSucceeded
AnswerProduced
RunCompleted
```

со `stream_version` от 0 до 9 и `latest_stream_version = 9` (версия последнего
события, не количество событий — эти два числа совпадают только случайно,
когда версии начинаются с 0 без пропусков).

`nodes` сохраняют canonical `event_id`, `stream_version`, `global_position`,
payload и metadata. `edges` создаются только из явного `causation_id`, wire
формат — ровно `source_event_id`/`target_event_id`/`kind: "caused"`;
`cause_event_id`/`effect_event_id`/`kind: "causes"` — предыдущая, более не
эмитируемая форма, схема v1 больше не совместима с ней. `timeline` — список
объектов `{event_id, stream_version, global_position}`, упорядоченный по
`stream_version`; `global_position` в `timeline` остаётся database ordering
metadata и не определяет run-local порядок (может быть неконтигуозным).
Missing cause хранится в `dangling_causation`, неизвестные event types не
отбрасываются.

Это первый зафиксированный контракт (`schema_version = 1`). Опубликованной
версии ещё не было, поэтому legacy alias для старого edge/timeline формата не
добавляется — `trace_to_json()` эмитирует только финальную форму.

Trace — disposable projection immutable log, а не отдельная таблица, checkpoint
или cryptographic tamper evidence.

## SSE contract

SSE endpoint:

```http
GET /agents/{agent_name}/runs/{run_id}/stream
Last-Event-ID: {stream_version}
```

- `id` каждого SSE record равен сохранённому `stream_version` — run-local,
  непрерывный cursor для этого одного run.
- `Last-Event-ID` при reconnect означает "последняя увиденная client'ом
  `stream_version` этого run"; сервер отдаёт хвост после неё.
- `global_position` в SSE не участвует — это internal database ordering,
  используемый workers и global subscriptions (checkpoints), не UI cursor.
- In-memory notification — только wake-up hint ("возможно, есть новое"), не
  source of truth. SSE handler всегда перечитывает persisted history из store
  после пробуждения; notification, которая потерялась, продублировалась или
  пришла до or во время текущего read/send цикла, не приводит к потере событий.
- Generation counter (`_Broadcaster.wait_for_change_since`) снимает snapshot
  *до* чтения store, а не в момент вызова wait — это устраняет race, при
  котором notify(), пришедший во время чтения/отправки текущего батча, был бы
  молча поглощён как новый baseline и заставил бы соединение ждать полный
  poll timeout вместо немедленного пробуждения.
- Reconnect отдаёт ровно события после `Last-Event-ID`, ни больше, ни меньше.
- Завершённый (terminal) run: SSE реплеит полную сохранённую историю и сам
  закрывает соединение — не виснет в ожидании notifications, которых больше
  не будет.

## Будущий runtime correlation

Это контракт-кандидат, а не описание уже реализованной схемы Flow Xray:

```json
{
  "runtime_trace_id": "trace-456",
  "runtime_call_id": "runtime-call-17",
  "stream_id": "energy-assistant:run-123",
  "input_event_id": "event-that-requested-the-effect",
  "operation_id": "same-stable-event-id",
  "output_event_ids": ["event-produced-by-the-effect"]
}
```

Обратные ссылки в Agentlog следует добавлять как metadata новых событий, не
переписывая старые:

```json
{
  "causation_id": "input-event-id",
  "runtime_trace_id": "trace-456",
  "runtime_call_id": "runtime-call-17"
}
```

Agentlog не навязывает Flow Xray внутренний runtime-call формат.

## Acceptance scenario runtime correlation

Для одного durable effect Flow Xray должен вычислимо показать:

```text
input domain event
→ runtime call subtree
→ output domain event(s)
```

Проверка должна сопоставить IDs в обе стороны; визуальное сходство графов не
считается доказательством.
