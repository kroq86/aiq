# Reference chat-to-MCP agent

Это главный acceptance scenario проекта, а не дополнительная демонстрация.

## Пользовательская история

Пользователь отправляет:

```text
Покажи давление A-17 за последние сутки.
```

Есть ровно два валидных entry point, с разной длиной canonical stream. Это не
противоречие — это два разных способа создать первый event потока.

### Non-HTTP flow (9 events)

`examples/chat_mcp_agent/main.py` пишет `UserMessageAdded` напрямую в store,
минуя HTTP command boundary:

```text
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

- 9 events, `stream_version` от 0 до 8;
- начинается сразу с `UserMessageAdded`;
- не содержит `RunCreated`, потому что не проходит через HTTP command layer.

### HTTP flow (10 events)

`examples/http_chat_agent/main.py` через `POST /agents/{agent_name}/runs`
(`aiq.http.create_app`) атомарно пишет `RunCreated` + `UserMessageAdded`
как часть command handling (`http.py::create_run`):

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

- 10 events, `stream_version` от 0 до 9;
- `RunCreated` — persisted HTTP command event, не побочный эффект;
- `latest_stream_version = 9` (версия последнего event, не количество событий).

Оба сценария подтверждены исполняемым запуском (`PYTHONWARNINGS=error
PYTHONPATH=src python3 -m unittest discover -s tests -v`, плюс прямой запуск
обоих `examples/*/main.py`), а не только документацией.

Effect request metadata:

```text
operation_id = request.event_id
causation_id = preceding domain event_id
```

Corresponding `*Succeeded`, `*Failed` or `*Rejected` result metadata:

```text
operation_id = request.operation_id
causation_id = request.event_id
```

Оба значения находятся в immutable SQLite history, а не существуют только как
аргументы adapter call.

Полный путь:

```text
user input
→ durable model intent
→ model selects get_well_pressure
→ durable tool intent
→ MCP call
→ durable tool result
→ next model step
→ final answer
```

## Executable specification

Пример использует SQLite, fake LLM и fake MCP и не требует API keys:

```bash
PYTHONPATH=src python3 examples/chat_mcp_agent/main.py
```

Другой путь базы можно передать первым аргументом:

```bash
PYTHONPATH=src \
python3 examples/chat_mcp_agent/main.py /tmp/aiq-chat-demo.db
```

## Что доказывает acceptance test

- model request записан до LLM adapter call;
- model result записан до интерпретации;
- tool request записан до MCP adapter call;
- MCP получает стабильный operation ID request event;
- tool result сохранён до следующего model step;
- answer и terminal lifecycle event находятся в stream;
- повторный catch-up на idle runtime не вызывает adapters снова.
- dispatcher не исполняет reactions/effects для stream другого agent namespace.

## Failure scenarios

### Commit failure после внешнего результата

```text
adapter вернул результат
→ checkpoint write искусственно падает
→ вся SQLite transaction откатывается
→ restart повторяет adapter с тем же operation ID
→ canonical stream содержит один result event
```

### Domain failure

```text
MCP возвращает well_not_found
→ handler возвращает ToolCallFailed
→ failure event и checkpoint коммитятся
→ subscription продолжает работу
```

### Unknown tool

```text
model выбирает незарегистрированный tool
→ ToolCallRejected
→ AnswerProduced
→ RunCompleted
```

Unknown tool не превращается в бесконечный infrastructure retry.

### Invalid model output

Неизвестный `response.type` превращается в:

```text
ModelOutputRejected
→ AnswerProduced
→ RunCompleted
```

Невалидный model result не блокирует reaction subscription.

### Terminal run

После `RunCompleted`, `RunFailed` или `RunCancelled` поздние input events не
создают новые reactions или effects.

## HTTP-граница

`aiq.http.create_app()` открывает этот сценарий наружу:

```text
POST /agents/{agent_name}/runs
→ run_id
→ durable processing (background catch-up loop)
→ GET .../stream (SSE, Last-Event-ID = stream_version)
→ GET .../runs/{run_id} (текущее состояние)
→ GET .../runs/{run_id}/trace (versioned domain-event graph)
```

SSE читает immutable log через `store.load(after_version=...)` после каждого
пробуждения; in-memory notification — только сигнал "возможно, есть новое",
не источник истины. Исполняемый пример: `examples/http_chat_agent/main.py`.

`create_app()` — standalone convenience поверх `aiq.fastapi.AIQ`,
не единственная модель. Для встраивания в уже существующее приложение (свой
lifespan, свои routes) — `AIQ` напрямую:
`examples/embedded_fastapi/main.py`, контракт в
[docs/fastapi.md](fastapi.md).

`GET /agents/_health` — framework-owned liveness/readiness endpoint:
`503`, если background dispatcher упал (`status="unhealthy"`), иначе
`200`. Мёртвый worker не остаётся незаметным, пока HTTP продолжает
отвечать; полный health/shutdown контракт — там же, в
[docs/fastapi.md](fastapi.md).

### Следующая граница

- `run_forever()` catch-up вместо poll-цикла внутри `http.py`;
- multi-agent/multi-process deployment;
- token-level streaming (сознательно не в MVP, см. `docs/effects.md`).
