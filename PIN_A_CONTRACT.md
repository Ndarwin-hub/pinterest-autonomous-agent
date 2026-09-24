# Pin A Universal Trigger Contract

## Canonical command routing

`Run pin a` and `Run pin A` are the same canonical command. Every authorized AI agent or other source connected through Composio must route this command to the universal Pin A trigger (`PINTEREST_RUN_PIN_A`, with `PINTEREST_PIN_A` retained as a legacy alias). No caller may create a competing scheduler/batch path for this command.

The universal command-level Pin A trigger remains Railway `POST /pin-a`, where the Railway scheduler and daily ledger remain the execution authority. Scheduled execution may also directly activate the same scheduler through `POST /amazon/run-batch`; this is not a second scheduler or execution owner. This includes ChatGPT, Claude, Grok, Gemini, other AI agents, Composio-connected tools, GitHub, Railway, Cloudflare, and future authorized callers.

Pin A is a platform-independent trigger/wake layer. Existing scheduled mechanisms remain in place and may activate Pin A at their normal scheduled times. Pin A can also be invoked independently on demand.

## Trigger sources

Pin A accepts authorized requests from any caller that can satisfy the authentication contract. The caller source is metadata, not a hard-coded allow-list. Examples include:

- GitHub / GitHub Actions / GitHub Cron
- Railway Cron
- Cloudflare Watchdog
- Composio
- ChatGPT, Claude, Grok, Gemini, or other AI agents
- Future automation platforms
- Authorized manual/API callers

## Current transport

The canonical Railway endpoint is:

POST /pin-a

Authentication:
- X-Pin-A-Secret using an authorized configured scheduler/API secret, or
- X-Scheduler-Secret using an authorized configured scheduler/API secret, or
- GitHub Actions OIDC from the authorized repository/main branch for schedule/workflow_dispatch.

Informational headers:
- X-Pin-A-Source
- X-Pin-A-Request-ID
- X-Pin-A-GitHub-Run-ID
- X-Pin-A-Event

## Scheduled behavior

Existing scheduled mechanisms are not replaced. A scheduled trigger continues performing its existing action and also activates Pin A.

Current GitHub scheduled Amazon triggers exercise Pin A as a non-blocking compatibility wake, then directly activate the existing /amazon/run-batch scheduler path. A temporary Pin A/gateway failure cannot abort the mandatory scheduler activation.

Current Railway Cron likewise attempts Pin A first for compatibility, but Pin A failure is non-fatal; it always continues with the mandatory /amazon/run-batch activation.

Both wake and scheduler requests are intentionally idempotent because Railway's scheduler/ledger remains the execution authority. Repeated requests are acceptable and do not create competing batch ownership.

## Independent manual behavior

Pin A can be invoked at any time without waiting for a scheduled trigger. The GitHub Pin A workflow provides a manual workflow_dispatch entry point, and the Railway /pin-a endpoint provides the stable platform-independent API contract for other authorized callers.

## Execution ownership

Pin A does not own individual batch selection or replace the existing scheduler.

One accepted Pin A wake asks the existing Railway scheduler to start/wake the daily automation session. The Railway scheduler and daily ledger remain the source of truth for which batches are due and for duplicate prevention.

If several scheduled or manual sources trigger Pin A close together, they must converge on the same Railway scheduler session rather than create competing sessions.

The existing /submit workflow, Pin [number], image-selection system, and other automation paths remain separate.

## Portability

Callers should depend on the Pin A logical operation and contract, not on GitHub-specific batch semantics. The layer can therefore be moved or exposed directly from Railway without redesigning the callers.
