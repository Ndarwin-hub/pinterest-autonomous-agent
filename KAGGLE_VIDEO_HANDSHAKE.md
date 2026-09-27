# Kaggle ↔ Railway video handshake

This interface is additive to the existing Pinterest automation. It does not change Pin A, Pin N, `/submit`, the four-Pin workflow, board routing, or recovery semantics.

## Ownership

- Pinterest schedule: only the opportunity/checkpoint.
- Kaggle: primary video generator and source of its own run status.
- Railway: persistent fallback pair owner.
- Composio/WoopSocial: independent publication verification.
- Railway pair pointer: `1+2 -> 3+4 -> 5+6 -> 7+8 -> 9+10 -> repeat` and advances only after Railway processes its own pair.

## Kaggle run identity

Every Kaggle run must use a unique `run_id`. Its owner/claim IDs should be `kaggle-<run_id>` so existing `/video/job/{batch}/{slot}/claim` and `/video/job/{batch}/{slot}/status` calls are attributable to one run.

The durable Railway database records:

- run ID
- day
- pair
- heartbeat
- RUNNING / COMPLETED / FAILED / INCOMPLETE
- per-product render/quality/publishing state
- per-platform results

A heartbeat older than `KAGGLE_VIDEO_HEARTBEAT_STALE_SEC` (default 900 seconds) is stale and cannot keep Railway waiting forever.

## Publication verification marker

Kaggle video captions must include these machine-readable markers:

`[video-run:<run_id>][asin:<ASIN>][batch:<batch>]`

WoopSocial publication verification searches the exact project for these markers. This prevents an old post from satisfying a new run.

## Decision

At the normal second-batch checkpoint for Railway's current pair:

1. Fresh Kaggle `RUNNING` -> Railway exits without rendering.
2. Kaggle `COMPLETED` + Composio confirms the expected publications -> Railway does nothing and advances its pointer.
3. Kaggle `FAILED`, `INCOMPLETE`, or stale -> Railway owns its current pair.
4. Kaggle `COMPLETED` but Composio verification is unavailable -> Railway does not blindly duplicate the run; it records `kaggle_completed_unverified` for explicit recovery/verification.
5. Kaggle `COMPLETED` but Composio shows incomplete publication -> Railway owns its current pair and preserves already-published platform results so it only targets missing/failed platforms where those results are known.

Recovery/unfinished Pinterest processing is never a video trigger.

## Storage

Railway processes one product at a time. Source images, FFmpeg intermediates, audio intermediates, and the finished local MP4 are deleted after the publishing operation completes or fails. Durable state remains in SQLite.
