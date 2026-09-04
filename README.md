# Pinterest Autonomous Agent

Cloud-hosted agent that accepts a single product URL, researches the product, generates 5 substantially different SEO-optimized Pinterest Pins, and publishes them using Composio-connected Pinterest tools.

**Architecture**

```
Joey / Client
    |
    | POST /submit {"url": "EXACT_PRODUCT_URL"}
    v
Railway FastAPI service (background job)
    |
    | Composio tools (dynamic)
    v
Pinterest (Pins published with exact original URL as destination link)
```

The original URL is **never** modified, shortened, or replaced.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/submit` | Accept product URL, return `job_id` immediately |
| GET | `/status/{job_id}` | Poll progress and final result |

### Submit example

```bash
curl -X POST https://YOUR-RAILWAY-URL/submit \
  -H "Content-Type: application/json" \
  -H "X-API-Secret: YOUR_SECRET" \
  -d '{"url": "https://example.com/product"}'
```

Response:
```json
{
  "job_id": "uuid",
  "status": "queued",
  "message": "Job accepted. Processing continues even if client disconnects."
}
```

### Status example

```bash
curl https://YOUR-RAILWAY-URL/status/JOB_ID -H "X-API-Secret: YOUR_SECRET"
```

## Railway Environment Variables

Set these in the Railway project:

| Variable | Required | Description |
|----------|----------|-------------|
| `COMPOSIO_API_KEY` | Yes | Composio API key so the service can execute Pinterest / research tools |
| `COMPOSIO_ENTITY_ID` | No | Defaults to `default` |
| `API_SECRET` | Recommended | Shared secret for `X-API-Secret` header |
| `PORT` | Auto | Provided by Railway |
| `LOG_LEVEL` | No | INFO / DEBUG |

## Behavior

- **Single product URL** → 5 different Pins (different title, description, creative angle) all linking to the exact same original URL.
- **Multi-product page** (Amazon Best Sellers, category, search, etc.) → attempts to identify individual products; currently falls back to treating the page as source while still using the original URL as destination when individual links cannot be reliably extracted.
- Board selection: lists existing boards via Composio and prefers a relevant one. Does not create boards unless none exist and policy is updated.
- Image: prefers `og:image` or product images from the page. Image-generation tools (Gemini etc.) can be wired in when available in the Composio entity.
- Background processing: job continues after the HTTP client disconnects.
- Duplicate protection and retries are handled with tenacity; successful Pins are not re-published in the same job.

## Local development

```bash
pip install -r requirements.txt
cp .env.example .env   # fill COMPOSIO_API_KEY and API_SECRET
uvicorn main:app --reload --port 8000
```

## Deployment notes

This repository is ready for Railway:

1. Create a new Railway project and connect this GitHub repo.
2. Set the environment variables above.
3. Deploy. The start command is already defined in `Procfile` / `railway.toml`.

After deployment, the service exposes `/health`, `/submit`, and `/status/{job_id}`.

## Current limitations (external)

- Image generation tools must be available and enabled in the Composio entity used by the Railway service.
- Rich product extraction (Firecrawl etc.) requires those toolkits to be connected to the same entity.
- At least one Pinterest board should exist (or the agent can be extended to create one).

Once the above are satisfied and `COMPOSIO_API_KEY` is set, the full flow works end-to-end.
