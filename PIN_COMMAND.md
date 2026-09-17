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
- The existing workflow researches the product, creates the five-Pin workflow, publishes, and verifies Pins.
- Existing durable publication idempotency and image-diversity guards remain in the workflow.
- Manual `/submit` behavior remains unchanged.

## Railway endpoint
`https://web-production-dae68.up.railway.app/submit`

## MCP bridge
The Railway service contains the Composio Custom MCP bridge. Its exposed tool is `PINTEREST_SUBMIT_URL`; the bridge forwards the exact URL to `/submit` with the configured Railway API secret. The bridge is intentionally a thin control surface and does not duplicate the Pinterest workflow.

## Important
Do not shorten, rewrite, strip affiliate parameters, substitute another product URL, or bypass `/submit` when using this command.
