# Railway Ollama Trading Bot v0021 COMPLETE REBUILT

Пересобрано заново после ошибки с архивами. Внутри bot.py реально:
`BOT_VERSION = 0066`.

## Что добавлено по сравнению с v0010

### v0011
- Trade Management: Breakeven, Trailing Stop, Partial TP.
- Position Monitor `/positions`.
- Smart Cooldown.

### v0012
- Real execution adapter для MEXC/BingX через ccxt.
- Только isolated margin.
- `/real_on`, `/real_off`.
- Market orders.
- Best-effort SL/TP reduce-only.

### v0013
- Auto Scanner Top.
- Интервалы: 15m / 60m / 4h / 12h / 24h / OFF.

### v0014
- Structural Layers:
  - OFF
  - Trendline Layer
  - Trendline + RS/BTC
  - Trendline + RS/BTC + Super Volume
  - Structural Only

### v0015
- Extended TP Mode.
- Включается только при Trendline + RS/BTC + Super Volume + AI confidence HIGH/80%+.

### v0016
- STOP ALL.
- Position Sync.
- `/stopall_on`, `/stopall_off`.
- `/positionsync_on`, `/positionsync_off`, `/positionsync_now`.

### v0017
- Strict AI Mode по умолчанию ON.
- Если AI не ответил — сигнал и execution блокируются.

### v0021
- `/strictai_on`
- `/strictai_off`

## Railway fix
Dockerfile содержит `zstd`, чтобы Ollama installer не падал на Railway.


## v0021 Ollama API Chat Fix

Минимальный фикс:
- `call_ollama()` теперь использует `/api/chat`
- старый `/api/generate` убран

Остальная логика не менялась.


## v0021 Runtime Fixes

Минимальные исправления:
- Ollama 404 теперь обрабатывается как возможное отсутствие модели: бот пытается `ollama pull`.
- При выборе Ollama-модели отправляются уведомления в Telegram: 10%, 50%, 100%.
- `/ping` снова показывает:
  - время отклика,
  - время работы,
  - память,
  - модель ИИ,
  - отклик/работу модели ИИ,
  - API биржи OK/ошибка.
- Ответы бота снова прикрепляют inline-меню, чтобы кнопки не пропадали.


## v0021 UI / Model / Status Fix

Минимальные исправления:
- В OpenAI Model добавлены варианты GPT-5.5.
- Position Sync toggle переключается через актуальное состояние настроек.
- Ответы по кнопкам теперь отправляются новым сообщением ниже, а не редактируют старое сообщение сверху.
- Под новыми ответами прикрепляется inline-меню.
- В Status добавлено явное поле `Selected Top Signal: Top-N`.


## v0021 Scan Progress Fix

Минимальное исправление:
- При запуске Top-50 / Top-100 / Top-200 бот пишет прогресс в чат:
  - 10% просканировано
  - 50% просканировано
  - 100% просканировано

Остальная логика не менялась.


## v0021 Single Work Message

Минимальное изменение интерфейса:
- Кнопки / меню / статус / ping / scan / загрузка модели обновляют одно активное рабочее сообщение.
- AI Chat Mode не затронут: ответы ИИ продолжают приходить отдельными новыми сообщениями.
- Торговые сигналы не затронуты: сигналы остаются отдельными новыми сообщениями.
- Версия бота обновлена до 0021.


## v0021 Hotfix AI/Layout

Минимальные исправления:
- Ollama AI call теперь пробует fallback endpoints:
  - `/api/chat`
  - `/api/generate`
  - `/v1/chat/completions`
- `/ping` AI health тоже проверяет несколько endpoints.
- Ошибка OpenAI без ключа теперь понятнее: нужно либо Ollama, либо `/setopenai`.
- Сигналы/ошибки по ручному вводу BTC/ETH теперь отправляются отдельным сообщением без прикрепления меню, чтобы не появлялось ощущение, что сообщение "над кнопками".


## v0021 Strict Signal Format

Добавлено:
- Жёсткий формат сигналов без воды.
- Бот сам считает ENTRY / SL / TP1 / TP2 / RR.
- AI больше не придумывает уровни.
- AI отвечает только APPROVED / REJECTED + confidence + короткая причина.
- Trendline/Structural setups получают TP profile RR примерно 1:4.
- Обычные сделки получают стандартный TP profile примерно 1:2.


## v0022 RR Logic Update

Новая логика тейков:
- Обычный сигнал -> RR 1:2
- Просто Trendline -> RR 1:2.5
- Trendline + RS/BTC -> RR 1:3
- Trendline + RS/BTC + Super Volume -> RR 1:4
- Structural Only -> RR 1:4 только если все 3 слоя подтверждены


## v0024 Inline Menu

Изменения:
- Основное меню переведено на inline-кнопки под сообщением.
- Добавлена команда `/menu` для повторного вызова inline-меню.
- Добавлена inline-кнопка `🧠 Ping AI`.
- `📡 Ping` остаётся быстрым, `🧠 Ping AI` проверяет модель отдельно.
- Сигналы и AI Chat остаются отдельными сообщениями ниже.


## v0026 Multi TF + Live Trade Manager

Изменения:
- Multi timeframe теперь реально: 15m + 1h + 4h + 1d.
- MTF проверяет всю цепочку, конфликты режут score, подтверждения добавляют bonus.
- Добавлен Live Trade Manager ON/OFF, по умолчанию OFF.
- Добавлены команды:
  - /livetrademanager_on
  - /livetrademanager_off
- Добавлена кнопка Live TM.
- Добавлен фоновый loop Live Trade Manager.
- Важно: Live Trade Manager пока безопасно ведёт локальное состояние сопровождения позиции; реальные modify/partial close ордера требуют отдельной биржевой донастройки reduceOnly/SL params.


## v0027 Live Trade Manager Connection Fix

Исправлено:
- Live Trade Manager loop теперь подключён в post_init и реально запускается в фоне.
- Команды зарегистрированы:
  - /livetrademanager_on
  - /livetrademanager_off
  - /livetrademanager_status
- Кнопка Live TM переключает настройку.
- По умолчанию Live TM = OFF.
- Loop проверяет локальные позиции и отмечает BE / partial TP / trailing / runner события.
- Реальные reduceOnly ордера на бирже всё ещё требуют отдельного безопасного adapter-теста.


## v0028 Live TM Real Execution

Добавлено:
- Live TM теперь может выполнять реальные действия, но только если:
  - Live Trade Manager = ON
  - Real Execution = ON
  - API ключи биржи заданы
- BE: пытается перенести SL в entry.
- TP1: пытается закрыть 50% reduceOnly market.
- После TP1: включает trailing и пытается обновлять SL.
- TP2: пытается закрыть остаток reduceOnly.
- Защита от повторных действий через tm-флаги:
  - be_done
  - partial_done
  - trailing_active
  - runner_done

Важно:
- По умолчанию Live TM OFF.
- По умолчанию Real Execution OFF.
- Реальные SL/stop params у MEXC/BingX через ccxt могут отличаться, поэтому тестировать только минимальной позицией.


## v0029 Live TM Notifications + STOP ALL PRO

Добавлено:
- Telegram уведомления Live TM:
  - TP1 reached
  - 50% closed/planned
  - SL moved to BE
  - Trailing Stop activated
  - Trailing updated
  - TP2 reached / Runner closed
- STOP ALL теперь аварийный:
  - выключает Auto Scanner
  - выключает Trading
  - выключает Real Execution
  - выключает Live TM
  - выключает Position Sync
  - пытается закрыть отслеживаемые позиции через reduceOnly, если Real Execution был ON
- Повторное нажатие STOP ALL выключает режим и возвращает безопасные дефолты:
  - Auto Scanner OFF
  - Trading OFF
  - Real Execution OFF
  - Live TM OFF
  - Position Sync OFF


## v0030 Hybrid Trendline + Full Help

Добавлено:
- Hybrid trendline:
  - текущий structure breakout detector сохранён;
  - добавлен 3-touch trendline bonus;
  - если найдено 3+ касания и breakout, добавляется bonus и reason.
- /help полностью обновлён:
  - /menu
  - /status
  - /ping
  - /ping_ai
  - /trading_on/off
  - /real_on/off
  - /livetrademanager_on/off/status
  - /stopall_on/off
  - /top50 /top100 /top200
  - /positions
  - все новые режимы, RR, Multi TF, Live TM, STOP ALL.


## v0031 Live TM Status Fix

Исправлено:
- Добавлен отсутствующий handler `livetrademanager_status_cmd`.
- Исправлен crash-loop при старте из-за NameError.
- `/livetrademanager_status` показывает Live TM, Real Execution, Exchange и tracked positions.


## v0032 Inline Buttons Fix

Исправлено:
- Добавлен/подключён `CallbackQueryHandler(inline_button_router)`.
- Inline-кнопки теперь обрабатываются отдельным стабильным router.
- Исправлены действия кнопок:
  - Signal / AI Chat
  - Provider / Model / Reasoning / Exchange
  - Trading / TF / Auto Scanner / Structural / Trade Mgmt
  - Top-50 / Top-100 / Top-200
  - Status / Ping / Ping AI
  - Positions / STOP ALL / Position Sync / Live TM
  - All Market / Sessions / Help
- При ошибке кнопки бот теперь присылает `Button error`, а не молча зависает.


## v0033 WAIT Always Reject

Исправлено:
- Если core signal / bot direction = WAIT, AI verdict принудительно становится REJECTED.
- Конфликт `SIDE: WAIT` + `AI VERDICT: APPROVED` больше невозможен.


## v0034 Inline Callback Routing Fix

Исправлено:
- Удалены старые конфликтующие CallbackQueryHandler регистрации.
- Оставлен один обработчик: `CallbackQueryHandler(inline_button_router)`.
- Router напрямую обрабатывает все inline callback_data.
- Добавлена команда `/callback_test` для проверки inline callback.
- Если кнопка даст ошибку, бот отправит `Button error`.


## v0035 Bottom Inline Menu + Ollama Keep Alive

Изменения:
- Inline menu теперь пересоздаётся внизу после служебных действий.
- Старое menu-сообщение удаляется, новое отправляется последним.
- Сигналы и AI chat остаются отдельными сообщениями, история не затирается.
- Submenu-сообщения (Model/Provider/TF/etc.) остаются активным нижним меню.
- OLLAMA_KEEP_ALIVE по умолчанию: 10m.


## v0036 Auto AI Scan Progress

Исправлено:
- Ручной шаг `AI Confirm` убран из Top scan.
- Top-50/100/200 теперь сами отправляют кандидатов в AI.
- Скан пишет в чат:
  - `Сканирую монету ...`
  - `Просканировал 10%...`
  - `Просканировал 50%...`
  - `Просканировал 100%...`
- Загрузка Ollama модели пишет:
  - `Загружено 10%`
  - `Загружено 50%`
  - `Загружено 100%`


## v0037 No WAIT in Top Scan + Static Checks

Исправлено:
- Top-50/100/200 больше не отправляют WAIT в AI.
- Top scanner оставляет только LONG/SHORT до AI.
- AI Confirm дополнительно защищён от WAIT в старых LAST_SCAN_RESULTS.
- Одиночный анализ BTC/ETH при WAIT не вызывает AI, сразу REJECTED.
- В Top scan добавлена строка `WAIT skipped`.
- Проверены:
  - Python compile
  - все CommandHandler функции существуют
  - CallbackQueryHandler один
  - run_top_scan содержит hard skip WAIT


## v0039 Import + Telegram Bot Token Fix

Исправлено:
- Crash-loop `NameError: name 'Dict' is not defined`.
- Fallback menu functions перенесены после import-блока.
- Добавлен/проверен import `Dict, Any, Optional, List, Tuple`.
- В коде оставлен только `TELEGRAM_BOT_TOKEN`.
- `TELEGRAM_TOKEN` больше не нужен.
- Railway Variables: используйте только `TELEGRAM_BOT_TOKEN`.


## v0052
- Убран повторный вывод AI approval в Auto Scanner.
- В AI approval добавлены Scanner Score, AI Confidence и вероятность отработки.
- Prompt требует scanner_score и success_probability в строгом JSON.

## v0051
- Усилен AI approval prompt: ИИ больше не должен одобрять сделки только по score.
- Добавлена проверка structure, MTF, volume/RVOL, momentum, volatility и risk/reward перед APPROVE.

## v0050
- AI-send status now shows the number of LONG/SHORT candidates sent to AI.

## v0049
- Signal button removed. Auto Scanner ON now means signals ON; Auto Scanner OFF means signals OFF.
- TopLimit moved to main menu in place of Signal. Default is 5; options: 5 / 10 / ALL.
- AI approval uses the selected TopLimit.

### v0070
- Исправлено ложное определение OpenAI key: `/testai` больше не берёт глобальный `OPENAI_API_KEY` из Railway по умолчанию.
- Добавлена команда `/clearopenai` (алиасы: `/delopenai`, `/unsetopenai`) для удаления сохранённого ключа пользователя.
- Для намеренного использования общего ключа из env добавлен флаг `OPENAI_ENV_FALLBACK=1`.
