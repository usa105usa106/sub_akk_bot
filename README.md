## v0226 Realtime Positions No Cache hotfix

This build keeps SR Rebound and SR AI Medium/Hard, but restores slot accounting to realtime exchange-only reads. Local cache/rebuild/TP-SL placeholders are not used for slots.

## v0226 SR Rebound Auto Mode — 2TP + calibrated scoring + stable slots + SR AI Medium/Hard

Based on v0224 stable slots restore. This version changes only SR AI mode/prompt handling and version text. It does not change slot reading, positions sync, rotation, balance, or order execution.

New SR AI commands:

- `/sr_ai_medium` — default working AI filter for SR Rebound. Less strict than hard; blocks obvious bad trades but does not demand a perfect chart.
- `/sr_ai_hard` — strict SR Rebound AI filter, similar to the previous strict prompt; confirms only near-perfect A/A+ SR rebound setups.

`/sr_rebound_on` now automatically sets:

- `scan_mode = sr_rebound`
- `sr_ai_mode = medium`
- `scanner_size = 200`
- `auto_scanner_interval = 15m`
- internal TF: `15m` reaction + `1H/4H` levels
- `market_universe = all`
- `structural_mode = off`
- `top_limit = 10`
- `min_score = 75`

SR Rebound trade profile:

- SL: behind support/resistance level with ATR/zone buffer
- TP1 = 1R
- TP2 = 1.4R
- TP3 disabled

AI can still be globally enabled/disabled with:

- `/ai_on`
- `/ai_off`

`BOT_VERSION=0226`.

### v0226 changes

- Added `sr_ai_mode` setting, default `medium`.
- Added `/sr_ai_medium` command.
- Added `/sr_ai_hard` command.
- Added separate SR Rebound JSON approval prompts for Medium and Hard.
- Included SR context in compact AI candidates.
- Status/help now show SR AI mode.
- No changes to positions/slots/rotation/balance/execution.
