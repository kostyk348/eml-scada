# EML-SCADA v0.1

SCADA-стек на базе [mime-os / EMLBox](https://github.com/kostyk348/mime-os).
**Всё есть `.eml`**: историк = записи tagdb, станция = один контейнер,
сообщения контроллеров = MIME на IPC-шине, аларм-лог = дельты с hash-chain.

```
HMI (NiceGUI) ──► шина (спулы .eml) ──► движок ──► историк tagdb + станция.eml
симуляторы контроллеров (ctl_01..03)  ◄── SETPOINT/ACK ──►  шина
```

## Запуск

```bash
# headless-проверка (без GUI): 22с опроса, алармы, verify
python -m scada.run --store store --headless --seconds 22

# HMI: http://127.0.0.1:8080
python -m scada.run --store store --port 8080

# tamper-демо: правка дельта-блока в копии станции ловится verify
python verify_demo.py store
```

Требования: Python 3.11+ (`pip install nicegui`), собранный `emlbox` из mime-os
(путь берётся из `EMLBOX` или `scada/emlbox.py::DEFAULT_EMLBOX`).

## Что внутри (структура данных)

| Слой | Где | Формат |
|---|---|---|
| Историк | `store/hist/*.eml` | tagdb-запись: `X-Tag`=точка, `X-Timestamp`, body `{value, quality}` |
| Станция | `store/station_01.eml` | контейнер: KV-таблицы `config`, `state`, `alarms` (дельты) |
| Шина | `store/bus/*.msg.eml` | `From/To/X-Event` + JSON-тело |
| Алармы | `state/alarms` в контейнере + ALARM-события на шине | дельты hash-chain |

Протокол: `TELEMETRY` (контроллер → движок), `SETPOINT`/`SETPOINT_ACK`
(команды между контроллерами и HMI), `ALARM` (движок → HMI).

## Проверено (headless-прогон 22с)

- 8 живых точек, 181 запись историка
- 2 аларма HIGH (temp ctl_01), лог в контейнере, `verify` → OK
- SETPOINT flow=60 → ctl_02 применил → ACK вернулся на шину
- Tamper: флип байта в дельта-блоке копии → `BROKEN: delta 1 hash mismatch`

## Известные ограничения mime-os (найдено при интеграции)

1. `verify` не хэширует tail index и trailer — правка в хвосте не ловится.
2. Windows: тест `tamper_detected_by_verify` падает (mmap-лок); `pack` пишет
   пути с `\` вместо `/`. На Linux эти тесты зелёные.
3. Rust-runner (`emlbox run`) не используется — свой движок на Python.
