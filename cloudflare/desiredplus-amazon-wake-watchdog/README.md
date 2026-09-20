# Desired Plus Amazon Wake Watchdog

Cloudflare independently checks every minute in UTC and wakes the existing Railway
`POST /amazon/run-batch` endpoint for the 10 daily checkpoints.

UTC checkpoints:
- Batch 1: 00:23
- Batch 2: 01:11
- Batch 3: 01:59
- Batch 4: 02:47
- Batch 5: 03:35
- Batch 6: 04:23
- Batch 7: 05:11
- Batch 8: 05:59
- Batch 9: 06:47
- Batch 10: 07:35

Each checkpoint has a 5-minute recovery window. Repeated wake requests are safe because
Railway owns the daily ledger/session and rejects or reuses already-started work.

The Worker secret `CLOUDFLARE_WAKE_SECRET` must match the Railway environment variable
`CLOUDFLARE_WAKE_SECRET`. The Railway ledger remains the authoritative idempotency layer, so
GitHub, Cloudflare, and Railway watchdogs can safely converge on the same batch without
creating duplicate daily batch work.
