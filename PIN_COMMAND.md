# Pin URL command

## Canonical command/tool
`PINTEREST_SUBMIT_URL`

## Input
```json
{"url":"<exact product-or-affiliate-url>"}
```

## Behavior
- Accept exactly one `http://` or `https://` product/affiliate URL.
- Preserve the URL unchanged as the Pinterest destination URL.
- Forward it to the existing Railway `POST /submit` workflow.
- The existing workflow researches the product, attempts the five-Pin workflow, publishes each Pin independently, and verifies each published Pin independently.
- Successfully published Pins are kept. There is no all-or-nothing rollback or unpublish step.
- Final job status is based on verified published Pins: `completed` for 5/5, `completed_partial` for 1-4/5, and `failed` for 0/5.
- A failed or unverified Pin is never falsely marked successful, but it does not invalidate or remove other Pins that were successfully published and verified.
- Existing durable publication idempotency and image-diversity guards remain in the workflow.
- Manual `/submit` behavior remains intact.
- The same result/status contract is used by manual `/submit`, scheduled Amazon automation, and the Composio-connected Amazon automation path because they all enqueue through the same job pipeline.

## Railway endpoint
`https://web-production-dae68.up.railway.app/submit`

## MCP bridge
The Railway service contains the Composio Custom MCP bridge. Its exposed tool is `PINTEREST_SUBMIT_URL`; the bridge forwards the exact URL to `/submit` with the configured Railway API secret. The bridge is intentionally a thin control surface and does not duplicate the Pinterest workflow.

## Important
Do not shorten, rewrite, strip affiliate parameters, substitute another product URL, bypass `/submit`, or unpublish successful Pins when using this command.
