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
- The existing workflow researches the product, attempts the four-Pin workflow, publishes each Pin independently, and verifies each published Pin independently.
- Successfully published Pins are kept. There is no all-or-nothing rollback or unpublish step.
- Final job status is based on verified published Pins: `completed` for 4/4, `completed_partial` for 1-3/4, and `failed` for 0/4.
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


## Pin N product-count command
- `Pin N` means N distinct Amazon US products, not N total Pins.
- Railway discovers N currently buyable Amazon US product-detail listings, rejects duplicate ASINs against the durable publication registry, injects/preserves affiliate tag `desiredplus-20`, and submits each accepted product to the existing Pinterest job pipeline.
- For each product, the existing image workflow prioritizes the best verified imagery in this order: native 12K/8K/4K imagery when available, then other verified high-quality imagery, then the native product-page image only as the final image-source fallback.
- Image URLs are verified by fetching the actual image and checking dimensions; the same image URL is not reused within a product's Pin set.
- A product is not discarded merely because fewer than four usable images are available. Every usable, individually verified Pin is published and retained. The product job is `completed_partial` when at least one verified Pin is published but fewer than four are available; zero verified Pins is failure.
- Railway waits for the product jobs and returns the actual per-product Pin counts and verification results.
- The existing manual `Pin + [product URL]` / `PINTEREST_SUBMIT_URL` path is unchanged.
- The Composio Custom MCP bridge exposes `PINTEREST_PIN_COUNT` for this command. `Pin N` remains a product-count command; each product now targets four Pins.
