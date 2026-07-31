# Versioned deployment

Этот документ — operational contract для того, кто **эксплуатирует**
agentlog-приложение, а не для того, кто его пишет. Он описывает, что
происходит с in-flight run-ами при деплое новой версии definition, и что
нужно сделать руками, потому что agentlog это не автоматизирует.

Всё, что здесь написано, доказано исполняемыми тестами
(`tests/test_e2e_scenarios.py::DefinitionVersionIsolationTests`,
`tests/test_runtime.py`'s `DefinitionMismatchError`/checkpoint-isolation
tests) — не просто задокументировано.

## `version` — opt-in, не обязательный

```python
Agent(name="support", version="1", ...)
```

Если `version` не задан (`None`) — **никакой изоляции нет**:

```text
version=None  =>  no definition isolation guarantee
```

Это явный контракт, а не забытая фича. Без версии agentlog не может
отличить "старую" логику от "новой" — run будет молча интерпретироваться
под тем definition-объектом, который сейчас запущен, каким бы он ни был.
Если вы вообще планируете менять логику агента после того, как в системе
уже есть незавершённые run — используйте `version`.

## Что происходит при rolling deploy

`v1` и `v2` могут работать одновременно, на одной SQLite базе, читая один
и тот же global event log:

```text
Runtime_v1  interprets only  Runs_v1
Runtime_v2  interprets only  Runs_v2
```

Технически это работает потому что:

- `RunCreated.data["definition_version"]` записывается один раз при
  создании run и больше не меняется;
- каждая версия имеет **собственный** subscription checkpoint
  (`{agent_name}:{version}:reactions` / `{agent_name}:{version}:effects`)
  — прогресс `v2` никогда не продвигает checkpoint `v1`, и наоборот;
- при встрече с run чужой версии dispatcher пишет в лог
  `"agentlog: skipping stream ... (blocked; not reprocessed automatically
  -- see DefinitionMismatchError)"` и продвигает **только свой**
  checkpoint — остальные run той же версии продолжают обрабатываться
  нормально (`Mismatch(r1)` не влечёт `Failure(r2)`).

Прямой доступ (`GET .../runs/{run_id}`, `POST .../commands/...`) к run
другой версии возвращает `409 Conflict`, а не тихо продолжает его под
неправильной definition.

## Как узнать, сколько run осталось на `v1`

Встроенного дашборда для этого нет — это честная граница текущего MVP, не
скрытая недоработка. Паттерн запроса (через store напрямую, вне HTTP
API):

```python
history = await store.load(stream_id)
run_created = history[0]  # RunCreated is always version 0, if present
version = run_created.event.data.get("definition_version")
```

Практически: держите список `run_id` (или сканируйте `RunCreated`-события
в global log по вашему собственному индексу/логу создания run), и для
каждого проверяйте `GET /agents/{agent}/runs/{run_id}` — `404` (run не
найден), `409` (принадлежит другой версии — оставьте `v1` воркер живым),
или ответ с `state`/terminal-статусом (`GET .../trace`'s `terminal_status`
— `"active"` значит всё ещё в работе).

## Когда можно остановить `v1` worker

Когда для всех run-ов, созданных под `v1`, `trace.terminal_status !=
"active"` — то есть каждый либо `completed`, либо `failed`, либо
permanently blocked (см. ниже — это тоже не "active", просто это не
прогресс, а тупик, который вам всё равно придётся разобрать руками).

## Permanently blocked run

Если `DurableDispatcher`/`DurableEffectDispatcher` встречают
`TerminalEventConflictError` (два terminal-события в одном batch — баг
самой definition, а не старая версия) — это **не** тот же случай, что
version mismatch. Это валит воркер (`agentlog: worker failed` в логах,
`/agents/_health` → `503`), и **рестарт не лечит** — конфликтующее событие
остаётся в store незакоммиченным навсегда, и новый воркер наткнётся на
тот же conflict немедленно. Нужен либо фикс кода (reducer/reaction
definition), либо ручное вмешательство в store (не автоматизировано,
осознанно).

## Чего здесь нет (осознанно)

- **Автоматической миграции/upcaster-а** между `v1` и `v2` — если
  history создана под `v1`, интерпретировать её под `v2` нельзя вообще,
  даже частично. Это уже в списке "Не реализовано" в README и не
  противоречит ему.
- **Дашборда** активных/blocked/failed run по версиям — см. выше, ручной
  query pattern, не встроенный инструмент.
- **Автоматического retirement** старого воркера — вы сами решаете,
  когда его остановить, основываясь на проверке выше.
