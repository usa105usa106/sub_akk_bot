# Railway Ollama Trading Bot v0018

## Что добавлено в v0010

- Кнопка `🟠 Only BTC/ETH`
- Режим рынка:
  - `All Futures Market`
  - `Only BTC/ETH`
- Если включен `Only BTC/ETH`:
  - Top-50 / Top-100 / Top-200 scanner отключается
  - анализ и торговля идут только по BTCUSDT и ETHUSDT
  - AI Confirm работает только по BTC/ETH
  - Auto/Confirm trade не откроет сделки по другим монетам
- Кнопка `📋 Статус`
- Кнопка `🕒 Таймфрейм`
- Кнопка `🌏 Азия/Америка`
- Режимы таймфрейма: `15 мин`, `15 мин/1 час`, `1 час/4 часа`, `мульти`
- Команда `/status`
- `/status` показывает все текущие настройки:
  - Bot version
  - AI provider
  - Active model
  - Reasoning level
  - Exchange
  - Bot mode
  - Trading mode
  - Trading enabled
  - AI auto confirm
  - Market universe
  - Scanner size
  - Min score
  - Top limit
  - Risk
  - Max risk
  - Max trades
  - Leverage
  - Model idle unload
  - OpenAI key status
  - Exchange API key status

## Railway env

Минимально:

```env
TELEGRAM_BOT_TOKEN=...
```

Опционально:

```env
BOT_VERSION=0018
DEFAULT_AI_PROVIDER=ollama
DEFAULT_OLLAMA_MODEL=llama3.1:8b
DEFAULT_OPENAI_MODEL=gpt-4.1-mini
DEFAULT_EXCHANGE=mexc
DEFAULT_REASONING_LEVEL=medium
DEFAULT_MARKET_UNIVERSE=all
DEFAULT_TIMEFRAME_MODE=15m
DEFAULT_SESSION_FILTER=off
DEFAULT_MIN_SCORE=80
DEFAULT_TOP_LIMIT=10
DEFAULT_SCANNER_SIZE=100
DEFAULT_RISK_PERCENT=1
DEFAULT_LEVERAGE=5
MODEL_IDLE_UNLOAD_SECONDS=1200
```

## Команды

```text
/start
/help
/status
/signal BTCUSDT
/top50
/top100
/top200
/minscore 80
/toplimit 10
/toplimit all
/onlybtceth_on
/onlybtceth_off
/market
/timeframe
/sessions
/sessions_on
/sessions_off
/model
/provider
/reasoning
/exchange
/mode
/tradingmode
/trading_on
/trading_off
/aiauto_on
/aiauto_off
/maxtrades 3
/maxrisk 3
/risk 1
/leverage 5
/setapi mexc API_KEY API_SECRET
/testapi
/delapi mexc
/setopenai sk-...
/testopenai
/delopenai
/ping
```

## Важно

API-ключи бирж создавай без withdraw permission.
Auto Mode связан с риском. Сначала тестируй Confirm Mode.

## v0010 Азия/Америка

Если включено, бот учитывает торговые сессии по МСК:
- Азия: 03:00
- Америка: 16:30

Это добавляется в `/signal`, Top Scanner, AI Confirm и отображается в `/status` и `/ping`.


## v0012 Real Execution Adapter

Добавлено:
- Реальное открытие market order для MEXC и BingX через ccxt.
- Только `isolated` margin.
- Команды:
  - `/real_on`
  - `/real_off`
  - `/positions`
- Перед входом бот пытается выполнить:
  - `set_margin_mode("isolated")`
  - `set_leverage(...)`
- Размер позиции считается от:
  - futures USDT balance
  - `/risk`
  - `/leverage`
- SL/TP ставятся best-effort через reduce-only trigger orders.
- Если биржа отклоняет SL/TP параметры, ошибка пишется в `/positions`.

Важно:
- API-ключи только с Futures Trading.
- Withdraw permission нельзя включать.
- Перед реальными деньгами проверь минимальными суммами.


## v0013 Auto Scanner Top

Добавлено:
- Кнопка `🔄 Auto Scanner Top`.
- Интервалы:
  - 15 мин
  - 60 мин
  - 4 часа
  - 12 часов
  - 24 часа
  - выкл
- По умолчанию выключено.
- Работает по текущим настройкам:
  - Scanner Top size
  - MinScore
  - TopLimit
  - Timeframe
  - Only BTC/ETH
  - Asia/America
  - AI Provider / Model / Reasoning
  - AI Confirm
  - Trading Mode / Trading ON
  - Real Execution ON/OFF

Важно:
- Чтобы Auto Scanner сам открывал сделки, нужно:
  - Trading Mode = Auto
  - `/trading_on`
  - `/aiauto_on`
  - для реальных сделок еще `/real_on`
- Если `/real_off`, бот будет писать PAPER.


## v0014 Structural Layers

Добавлена кнопка `🧠 Structural Layers`.

Режимы:
- `OFF` — работает как раньше.
- `Trendline Layer` — дополнительно ищет 3+ касания, compression, breakout pressure.
- `Trendline + Relative Strength vs BTC` — добавляет сравнение силы монеты относительно BTC.
- `Trendline + RS/BTC + Super Volume` — добавляет аномальный объем RVOL.
- `Structural Only` — отключает обычный scanner как главный фильтр и ищет только structural setups: trendline + RS/BTC + Super Volume.

По умолчанию: OFF.

Команда:
```text
/structural
```

Structural Layers учитываются в:
- `/signal`
- Top Scanner
- AI Confirm
- Auto Scanner Top
- Auto/Real execution decision pipeline


## v0015 Extended TP Mode

Добавлено:
- Автоматический Extended TP Mode.
- Включается только если:
  - Structural Mode = `Trendline + RS/BTC + Super Volume`
  - structural layer passed
  - AI confidence HIGH или >=80%
- Обычные сделки не меняются.
- AI Confirm помечает сделки `🚀 Extended TP`.
- `/signal`, `/status`, `/ping` показывают Extended TP status.
- По умолчанию включено, но активируется только при условиях выше.

ENV:
```env
DEFAULT_EXTENDED_TP_ENABLED=on
DEFAULT_EXTENDED_TP_MIN_CONFIDENCE=80
DEFAULT_EXTENDED_TP_RR=4
```


## v0016 STOP ALL + Position Sync

Добавлено:
- Кнопка `🚨 STOP ALL`.
- Кнопка `🔁 Position Sync`.
- По умолчанию оба выключены.

### STOP ALL
Первое нажатие:
- STOP ALL = ON
- Auto Scanner = OFF
- Trading = OFF
- Real Execution = OFF
- новые сделки блокируются

Второе нажатие:
- STOP ALL = OFF
- торговлю можно снова включить вручную через `/trading_on` и `/real_on`

Команды:
```text
/stopall_on
/stopall_off
```

### Position Sync
Сверяет локальные позиции бота с реальными позициями биржи через API.
Работает для MEXC/BingX best-effort.

Команды:
```text
/positionsync_on
/positionsync_off
/positionsync_now
```

ENV:
```env
DEFAULT_STOP_ALL_ENABLED=off
DEFAULT_POSITION_SYNC_ENABLED=off
DEFAULT_POSITION_SYNC_INTERVAL=300
```


## v0017 Strict AI Mode

Минимальное обновление безопасности:
- `STRICT AI MODE` включен по умолчанию.
- Если AI не ответил, вернул пустой ответ или явную ошибку:
  - сигнал блокируется;
  - AI Confirm блокируется;
  - Auto execution блокируется;
  - ручное открытие без AI-approved сделки блокируется.
- Остальная логика не менялась.

ENV:
```env
DEFAULT_STRICT_AI_MODE=on
```


## v0018 Strict AI Commands

Добавлены команды:
```text
/strictai_on
/strictai_off
```

STRICT AI MODE по умолчанию включен.
