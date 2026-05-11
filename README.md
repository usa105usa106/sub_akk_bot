# Railway Ollama Trading Bot v0020 COMPLETE REBUILT

Пересобрано заново после ошибки с архивами. Внутри bot.py реально:
`BOT_VERSION = 0020`.

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

### v0020
- `/strictai_on`
- `/strictai_off`

## Railway fix
Dockerfile содержит `zstd`, чтобы Ollama installer не падал на Railway.


## v0020 Ollama API Chat Fix

Минимальный фикс:
- `call_ollama()` теперь использует `/api/chat`
- старый `/api/generate` убран

Остальная логика не менялась.


## v0020 Runtime Fixes

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


## v0020 UI / Model / Status Fix

Минимальные исправления:
- В OpenAI Model добавлены варианты GPT-5.5.
- Position Sync toggle переключается через актуальное состояние настроек.
- Ответы по кнопкам теперь отправляются новым сообщением ниже, а не редактируют старое сообщение сверху.
- Под новыми ответами прикрепляется inline-меню.
- В Status добавлено явное поле `Selected Top Signal: Top-N`.


## v0020 Scan Progress Fix

Минимальное исправление:
- При запуске Top-50 / Top-100 / Top-200 бот пишет прогресс в чат:
  - 10% просканировано
  - 50% просканировано
  - 100% просканировано

Остальная логика не менялась.


## v0020 Single Work Message

Минимальное изменение интерфейса:
- Кнопки / меню / статус / ping / scan / загрузка модели обновляют одно активное рабочее сообщение.
- AI Chat Mode не затронут: ответы ИИ продолжают приходить отдельными новыми сообщениями.
- Торговые сигналы не затронуты: сигналы остаются отдельными новыми сообщениями.
- Версия бота обновлена до 0020.
