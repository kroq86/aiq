# Effect execution semantics

Этот документ является source of truth для effect boundary `aiq`.

## Effect delivery guarantee (читать в первую очередь)

Это единственное, что реально нужно знать перед тем, как писать effect
handler:

```text
handler executes once per commit-retry cycle
commit may retry (concurrent writer), never the handler

crash after the external call but before commit may repeat the effect
    -> exactly-once is NOT guaranteed
    -> use the stable operation_id for downstream idempotency
```

Расшифровка:

- **Handler выполняется ровно один раз за цикл диспетчеризации.** Если
  commit конфликтует с параллельным writer-ом (см. `_commit_outputs_with_retry`
  в `runtime.py`), повторяется только сам commit с уже вычисленным
  результатом — handler не вызывается заново. Это доказано
  исполняемыми тестами (`tests/test_runtime.py::test_version_conflict_retries_commit_without_reexecuting_effect`),
  не просто заявлено.
- **Но если процесс падает между внешним вызовом и commit-ом** (например,
  LLM/HTTP уже ответил, а SQLite transaction ещё не закоммичена), effect
  может быть вызван повторно после restart. Это `at-least-once`, не
  `exactly-once` — aiq **не может** и не обещает больше.
- Единственный инструмент против дублирования на стороне внешней
  системы — стабильный `operation_id` (`event_id` immutable request
  event, см. "Effect identity" ниже): передавайте его во внешний API,
  чтобы тот сам мог дедуплицировать повтор, если умеет.

## Граница

Reaction фиксирует решение как immutable request event:

```text
ModelCallRequested
ToolCallRequested
```

Effect handler выполняет внешний I/O и возвращает result events:

```text
ModelCallSucceeded | ModelCallFailed
ToolCallSucceeded  | ToolCallFailed
```

Effect handlers находятся в runtime-owned `EffectRegistry`, а не в
`AgentDefinition`.

## Гарантия

```text
at-least-once external execution
+
atomic result-event/checkpoint commit
```

Если внешний вызов завершился, но SQLite transaction не закоммичена, handler
может быть вызван повторно. Exactly-once для LLM, MCP, HTTP и других внешних
систем не обещается.

## Effect identity

Stable operation ID — `event_id` immutable request event, persisted in
canonical metadata:

```python
request = effect_request(
    "ToolCallRequested",
    data,
    metadata={"causation_id": cause_id},
)
assert request.metadata["operation_id"] == str(request.event_id)
```

Зарегистрированный effect request, созданный обычным `Event` без
`metadata.operation_id`, отклоняется до внешнего I/O. Конфликтующий
`operation_id` также отклоняется, а не перезаписывается.

Каждый result event handler'а нормализуется перед atomic commit:

```text
metadata.operation_id = request.metadata.operation_id
metadata.causation_id = request.event_id
```

User metadata сохраняется. Конфликтующие `operation_id` или `causation_id`
вызывают `EffectMetadataError`; result и checkpoint не коммитятся.

Operation ID создаётся до внешнего I/O, хранится в request/result events и
остаётся тем же при каждом retry и после SQLite reopen.

Если один request event описывает несколько независимых операций, каждая должна
получить отдельный стабильный operation ID в event data.

## Registry policy

Только event types, зарегистрированные в `EffectRegistry`, считаются effect
requests:

```python
@effects.effect("ToolCallRequested")
async def call_tool(...):
    ...
```

Обычное событие без handler пропускается, а effect checkpoint продвигается.
Naming convention `*Requested` сама по себе ничего не означает.

Если event должен быть effect request, отсутствие регистрации является ошибкой
конфигурации приложения. Core не пытается угадать это по имени.

## Failure policy

### Retryable или неизвестная ошибка

Handler выбрасывает exception:

```text
timeout
connection reset
temporary unavailable
unknown external outcome
```

Checkpoint не продвигается, result event не сохраняется, следующий запуск
повторяет handler.

### Terminal domain failure

Handler возвращает failure event:

```text
ToolCallFailed
ModelCallFailed
```

Примеры: `well_not_found`, `access_denied`, `invalid_arguments`,
`request_rejected`.

Failure event и checkpoint коммитятся атомарно. Subscription не блокируется
бесконечным retry.

## Atomic commit

Storage-level операция выполняет в одной SQLite transaction:

```text
BEGIN IMMEDIATE
→ validate expected checkpoint
→ validate expected stream version
→ append all result events
→ advance effect checkpoint
→ COMMIT
```

Ошибка любого шага откатывает все result events и checkpoint.

Fault-injection test создаёт SQLite trigger, который ломает checkpoint insert
после result inserts. Тест подтверждает:

- result event отсутствует после rollback;
- checkpoint не изменился;
- adapter вызывается повторно;
- operation ID остаётся тем же;
- после retry в canonical stream находится один result event.

## Durable dispatch-attempt telemetry

Опциональный `EffectAttemptStore` фиксирует operational fact непосредственно
после проверки canonical `operation_id` и перед входом в effect handler:

```text
validate effect request
-> append EffectDispatchAttempt
-> invoke handler
-> commit result events + checkpoint
```

Это не domain event и не часть atomic result/checkpoint transaction. Запись
означает только: dispatcher durably recorded an imminent handler invocation.
Она не доказывает, что downstream HTTP/MCP/provider call начался, завершился
или был дедуплицирован.

```python
attempt_store = await SQLiteEffectAttemptStore.open("aiq.db")
worker = DurableEffectDispatcher(
    ...,
    attempt_store=attempt_store,
)
```

Без `attempt_store` runtime работает как раньше и не платит дополнительную
стоимость. С настроенным store действует fail-closed invariant:

```text
HandlerInvocation => AttemptRecorded
```

Если append attempt не удался, handler не запускается, effect checkpoint не
продвигается, ошибка выходит из `run_once()`. Это сознательная
availability-for-evidence развилка: иначе committed handler result мог бы
существовать без attempt fact, а нулевой count нельзя было бы отличить от
потерянной telemetry.

Ограничения:

- crash после attempt commit, но до handler/downstream entry оставляет
  attempt-without-I/O и может завысить count;
- crash после внешнего эффекта, но до result commit приводит к следующему
  attempt с тем же `operation_id`;
- commit-only `VersionConflictError` не создаёт новый attempt, потому что
  handler не вызывается повторно;
- foreign streams, terminal runs и события без зарегистрированного handler не
  создают attempts;
- несколько workers без lease/single-flight могут честно записать несколько
  attempts для одной operation;
- SQLite telemetry добавляет отдельную durable transaction перед каждым
  handler invocation.

`build_run_report(..., effect_attempts=...)` агрегирует только явно переданные
records. `effect_attempts=None` означает «telemetry не наблюдалась», а пустой
tuple означает «наблюдалась, attempts не было». Эти метрики нельзя называть
точным physical-call count или provider dedup-hit count.

### Deployment contract

По умолчанию operational assumption — не более одного активного
`DurableEffectDispatcher` для canonical effect subscription конкретной версии
агента:

```text
{agent_name}:{definition_version}:effects
```

Несколько процессов с одним `subscription_name` могут одновременно прочитать
один pending request и вызвать handler до того, как один из них продвинет
общий checkpoint. Shared checkpoint защищает committed progression, но не
отменяет уже начатые внешние effects. Разные subscription names являются
независимыми consumers и вообще не должны использоваться как replicas одного
effect worker.

Для workers на одном общем SQLite-файле доступен opt-in fenced mode:

```python
integration = AIQ(
    store=await SQLiteEventStore.open("aiq.db"),
    runtimes=runtimes,
    lease_options=EffectLeaseOptions(
        worker_id="worker-instance-7",
        ttl_seconds=30,
        renewal_interval_seconds=10,
    ),
)
```

Claim создаёт новый `lease_id`, увеличивает fencing token и append
`EffectDispatchAttempt` в одной `BEGIN IMMEDIATE` transaction. Full-stream
terminal и уже committed result проверяются внутри этой transaction.
Непосредственно перед handler отдельная короткая transaction подтверждает
`lease_id + worker_id + token + status + DB expiry`. Renewal использует SQLite
DB time, сохраняет lease ID/token и меняет только expiry. Result events,
checkpoint и закрытие lease фиксируются одной transaction после повторной
проверки ownership. Stale/expired worker fail-closed: он не пишет ни output,
ни checkpoint.

`attempt_store` и `lease_options` нельзя задавать одновременно: host и
dispatcher выбрасывают `ValueError` при construction. В lease mode attempt
уже является частью atomic claim.

Если event store и старый `SQLiteEffectAttemptStore` используют один файл,
существующие `effect_attempts` сохраняются и numbering продолжается. Для
разных файлов автоматического merge нет: старый файл остаётся audit archive;
до включения lease pending effects нужно drain либо явно перенести ledger,
если требуется непрерывная нумерация.

Heartbeat зависит от cooperative asyncio scheduling. Blocking/CPU-heavy
handler может не дать heartbeat выполниться, вызвать ранний takeover и
двойное physical execution. Fenced commit всё равно отвергнет старого
владельца, но уже начатый внешний effect не отменит. Поэтому stable
`operation_id` и downstream idempotency обязательны.

### Lease observations

SQLite хранит append-only operational ledger только для
`claim_acquired`, `busy`, `expiry`, `renewal`, `takeover`,
`stale_ownership` и `stale_commit_rejection`. Каждая запись содержит
operation/request identity, worker, lease ID, fencing token и, когда был
создан attempt, его ID/number. Ledger пишется в той же coordination
transaction, которая устанавливает факт; он не управляет ownership.

Каждый busy poll и heartbeat renewal создаёт строку. Поэтому слишком короткий
renewal interval или contention storm увеличивает write amplification и размер
SQLite-файла. Это audit evidence, не точный downstream-call counter.

### Downstream idempotency contract

Один logical effect сохраняет один `operation_id` во всех physical retries.
Lease уменьшает конкурентные вызовы, но external execution остаётся
at-least-once. Application должна классифицировать effect:

- read-only;
- naturally idempotent;
- downstream idempotency-key aware;
- application-deduplicated;
- unsafe non-idempotent.

Первые четыре категории должны использовать стабильный `operation_id` как
ключ или вход собственного dedup protocol. Для unsafe non-idempotent effect
automatic retry требует явной application policy: запрета retry, human
confirmation либо осознанного принятия риска. AIQ не добавляет в этом
релизе универсальный `ToolExecutionContext` или compensation framework.

### SQLite operational limits

Все coordinated workers используют один SQLite-файл с WAL и корректными
filesystem locks. Coordination writes короткие, используют connection per
operation и `BEGIN IMMEDIATE`; handler никогда не выполняется внутри
transaction. SQLite database time authoritative, expiry inclusive:
`lease_expires_at <= database_now` означает stale. `busy_timeout=5000ms`
задаёт bounded storage wait, после которого ошибка выходит наружу; отдельный
unbounded retry loop не скрывает contention. Backup должен использовать
SQLite-safe snapshot/backup mechanism, а не копирование активного файла на
произвольной network filesystem.

## Dependency injection

Adapters передаются явно:

```python
context = EffectContext({
    "llm": llm_adapter,
    "energy_mcp": mcp_adapter,
})
```

Handler получает конкретный adapter через `context.require(...)`.
`EffectContext` не должен превращаться в неограниченный глобальный service
locator.

## Agent namespace isolation

Оба durable dispatcher по умолчанию используют canonical stream ownership:

```text
{agent_name}:{run_id}
```

Dispatcher обрабатывает domain logic и effects только для streams своего
agent name. Foreign global events не вызывают reducer, reactions или adapters;
они атомарно продвигают subscription checkpoint с пустым output batch, поэтому
mixed global log не блокирует catch-up.

Это compatibility break для прямых вызовов dispatcher со старыми
ненеймспейсными stream ID. Такие producers должны перейти на
`run_stream_id(agent_name, run_id)`. Параметр `owns_stream` остаётся явной
точкой расширения для специализированного routing; отключать isolation
неявно нельзя.

## Terminal runs

`AgentDefinition` может объявить terminal events:

```python
terminal_event_types={
    "RunCompleted",
    "RunFailed",
    "RunCancelled",
}
```

После terminal event reaction и effect dispatchers продвигают checkpoints, но
не создают новые domain actions и не выполняют внешний I/O.

`AnswerProduced` означает наличие ответа. `RunCompleted` означает официальное
завершение workflow. Эти факты не дублируют друг друга.

## No hidden operational state

Продуктовый инвариант, который эта durable-машина обязана держать:

> No future behavior may depend on information that is absent from
> persisted history or explicit resources.

То есть: `reducer`/`reaction`/`effect` handler получают ровно
`(state, event)` или `(effect, context)` — никогда сырую history и никогда
скрытый Python-объект снаружи этой пары. Если процесс убит и запущен заново
с нуля (новый `Agent`, новый `AgentRuntime`, новые dispatchers, новый
`context`), run обязан продолжиться идентично, используя только:

- persisted event log (`EventStore`);
- явно переданный `context`/resource-объект, тот же по контракту, не
  обязательно тот же Python-instance.

Это не философский тезис, а проверяемое свойство:
`tests/test_runtime.py`'s `test_resuming_a_run_from_a_fresh_agent_and_context`
строит run наполовину первым поколением `Agent`/`AgentRuntime`/dispatcher
объектов, полностью их выбрасывает, и завершает тот же run вторым,
независимо сконструированным поколением, читающим ту же SQLite-историю.

Честная граница гарантии:

- Она доказана для machinery самого framework (`AgentRuntime`,
  `DurableDispatcher`, `DurableEffectDispatcher`) — не для произвольного
  пользовательского кода. Пользователь всё ещё может спрятать состояние в
  module-level global, mutable default argument или closured переменной
  внутри своего `@agent.reduce`/`@agent.react`/`@agent.effect` — framework
  не может это обнаружить.
- "Explicit resources" — это тот объект, что host передаёт как `context` в
  `build_runtime(context=...)`. Сейчас это untyped `object` (см.
  `framework.py:281,298-312`) — типизация этого контракта остаётся
  отдельным, пока не сделанным пунктом.

## Не реализовано

- leases для разных database backends или разных SQLite-файлов;
- гарантия отсутствия overlapping physical calls после lease expiry;
- exponential retry schedule;
- точные downstream physical-call counters;
- timeout classification hierarchy;
- human approval для non-idempotent effects;
- компенсации;
- provider-specific idempotency.
