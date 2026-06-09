## v0221 SR Rebound Auto Mode — 2TP

Новая версия добавляет отдельный режим `SR_REBOUND`:

- `/sr_rebound_on` — включает полностью настроенный режим Support/Resistance First-Touch Rebound.
- `/sr_rebound_off` — выключает SR Rebound, возвращает Momentum и отключает Auto Scanner.
- `/scan_mode sr_rebound` — альтернативное включение через общий переключатель режимов.

При включении SR Rebound автоматически выставляет:

- `scan_mode = sr_rebound`
- `scanner_size = 200`
- `auto_scanner_interval = 15m`
- internal TF: `15m` реакция + `1H/4H` уровни
- `market_universe = all`
- `structural_mode = off`
- `top_limit = 10`
- `min_score = 75`

Режим не использует Momentum/Reversal/Structural layers. Другие scanner modes выключаются самим фактом установки `scan_mode=sr_rebound`.

AI можно менять отдельно обычными командами `/ai_on` и `/ai_off`.

`BOT_VERSION=0221`.


### v0221 changes
- SR Rebound TP3 disabled.
- SR Rebound uses two targets: TP1=1R, TP2=1.4R by default.
- BOT_VERSION updated to 0221.
