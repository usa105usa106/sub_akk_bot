v0230 realtime positions + SR mode check

- BOT_VERSION hardcoded to 0230 so Railway env BOT_VERSION cannot display stale 0226.
- /balance and slot accounting use live exchange positions only, no local cache as truth.
- SR_REBOUND keeps Hybrid label OFF, 2TP, no trailing, medium/hard AI.
