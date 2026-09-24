# Pin command contract

## 1. Run pin URL

Canonical command/tool: `PINTEREST_SUBMIT_URL`

Behavior:
- Accept exactly one HTTP(S) product/affiliate URL.
- Preserve the URL unchanged as the Pinterest destination URL.
- Forward it to the existing Railway POST `/submit` workflow.
- Research the product, create up to four Pins, publish each independently, and independently verify each published Pin.
- Keep successful Pins; never unpublish them because another Pin failed.
- `completed` = 4/4 verified, `completed_partial` = 1-3/4 verified, `failed` = 0/4.
- Manual `/submit` remains intact and isolated.

Railway endpoint: `https://web-production-dae68.up.railway.app/submit`

Do not shorten, rewrite, strip affiliate parameters, substitute another product URL, bypass `/submit`, or unpublish successful Pins.

---

## 2. Run pin N

`Run pin N` means N distinct Amazon US products, not N total Pins.
- Pin 1 = 1 product; Pin 2 = 2 products; ... Pin 10 = 10 products.
- Each accepted product targets four Pins.

### Primary execution
1. Start from the assistant/GitHub command path.
2. Request Pin-N work through the dedicated durable `/amazon/pin-count` endpoint.
3. Use a unique request ID and poll durable status.
4. Discover currently buyable Amazon US product-detail listings, exclude published ASINs, and preserve/inject `desiredplus-20`.
5. Send each accepted product through the existing shared Pinterest publication pipeline.
6. Verify actual Pinterest publication before reporting success.

### Railway-failure fallback
- `Run pin N` must not stop merely because Railway HTTP/proxy/deployment access temporarily fails.
- The assistant/GitHub layer is the fallback controller: retry safely, inspect durable status, and resume unfinished products without touching Pin A or manual `/submit`.
- When the production service is unavailable, continue from the GitHub side using the repository's existing executable workflow and configured credentials/capabilities when available.
- Never fabricate product discovery, Pin IDs, image verification, or publication status.
- Never consume Pin-A daily ledger slots or restart Batch 1.
- Never bypass shared publication guards.
- Requested products are successful only after actual Pinterest verification. Partial completion is never reported as success.

---

## 3. Unified latest Railway image-selection model

The current Railway image-selection model is the single shared image-selection contract for:
**Run pin URL → Run pin N / assistant-side execution → Railway fallback**.

Priority:
1. Composio Image Search native 8K+ imagery.
2. Composio Image Search native 4K+ imagery.
3. Other executable genuine image providers, ranked by verified resolution.
4. Existing executable AI image generation/editing, only when actually configured.
5. Verified 4K local upscale/derivative of the best genuine external image.
6. Native Amazon product-page imagery only as the final image-source fallback.
7. No placeholder or invented image.

Mandatory checks:
- Fetch actual image bytes before trusting dimensions.
- Prefer genuine 12K/8K/4K imagery when available.
- Reject broken, inaccessible, invalid, undersized, or visually duplicate candidates.
- Never reuse the same image URL within one product's Pin set.
- Register visual fingerprints and prevent near-duplicate reuse.
- AI/external reviewers are advisory only; they cannot by themselves reject an otherwise structurally valid Pin.
- Byte validation, dimensions, aspect ratio, and duplicate/fingerprint checks remain hard gates.
- If fewer than four valid images exist, publish every valid individually verified Pin instead of failing the whole product.
- Product-page imagery remains last resort; Pillow/placeholder images are never publishable.

### Single-source implementation rule
All three surfaces must enter the same shared image-selection/publication pipeline. No route may maintain a separate legacy image selector or bypass the current Railway image-selection model.

---

## 4. Isolation rules
- Pin A scheduler is unchanged; its daily ledger remains authoritative.
- Run pin URL remains the existing manual `/submit` route.
- Run pin N is isolated product-count execution and must not alter Pin-A scheduling or ledger ownership.
- GitHub is the command/fallback controller; Railway is the production execution service when available.
- Railway failure triggers recovery/retry, not a false success.
\n\n## Canonical Board Routing Rule\n- `Watches & Clocks` is the dedicated destination for watches, wristwatches, smartwatches, clocks, alarm clocks, wall clocks, desk clocks, and other timepieces.\n- Appliance products continue to route to `Home, Kitchen & Dining`; the old `Appliances & Home` board identity is no longer used by the automation.\n- ChatGPT, Grok, Gemini, Composio, Railway, GitHub, and Cloudflare must use the canonical resolver/board IDs rather than maintaining separate board maps.\n