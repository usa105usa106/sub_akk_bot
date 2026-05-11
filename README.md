# Railway Ollama Trading Bot v0018 COMPLETE REBUILT

Пересобрано заново после ошибки с архивами. Внутри bot.py реально:
`BOT_VERSION = 0018`.

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

### v0018
- `/strictai_on`
- `/strictai_off`

## Railway fix
Dockerfile содержит `zstd`, чтобы Ollama installer не падал на Railway.


## v0018 Ollama API Chat Fix

Минимальный фикс:
- `call_ollama()` теперь использует `/api/chat`
- старый `/api/generate` убран

Остальная логика не менялась.
