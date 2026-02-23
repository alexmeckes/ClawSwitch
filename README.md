# ClawSwitch

A cost-saving router for LLM APIs that runs entirely on your machine. No hosted service, no third-party proxy, no data leaving your network except directly to the LLM providers you choose.

You call one endpoint with `model: "claw-auto-cheap"`. ClawSwitch classifies your request by complexity (`SIMPLE` / `MEDIUM` / `COMPLEX` / `REASONING`), picks the cheapest model in that tier, and falls back to others if it fails. Same results, lower cost, full privacy.

**Everything runs locally.** Your API keys stay on your machine. Your prompts go straight from your machine to the provider — OpenAI, Anthropic, Mistral, etc. — with nothing in between that you don't control.

```
                  OpenClaw
                     │
┌────────────────────┼─────────────────────┐
│  your machine      │                     │
│                    ▼                     │
│            ClawSwitch (:4000)            │
│          classifies, picks cheapest      │
│                    │                     │
│                    ▼                     │
│          Any-LLM gateway (:8000)          │
│                    │                     │
└────────────────────┼─────────────────────┘
                     │
                     ▼
     OpenAI / Anthropic / Gemini / Mistral
```

## Install

There are two ways to run ClawSwitch. Both give you the same functionality.

### Option A: Docker (recommended)

Requires: Docker + Docker Compose

```bash
OPENAI_API_KEY=your-key ./scripts/install.sh
```

That's it. This creates config files, generates local keys, starts all services, runs a smoke test, and prints your connection settings. Pass `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`, or `GEMINI_API_KEY` instead (or in addition) for other providers.
Docker gateway version is controlled by `ANYLLM_GATEWAY_IMAGE` in `.env`.

To skip the smoke test:
```bash
SKIP_CHAT_SMOKE_TEST=1 OPENAI_API_KEY=your-key ./scripts/install.sh
```

Other commands:
```bash
make up          # start
make down        # stop
make logs        # tail logs
make doctor      # health check
make print-openclaw  # show connection settings
```

### Option B: Local (no Docker)

Requires: Python 3.11+, Postgres

1. Start Postgres if it isn't running:
```bash
# macOS
brew install postgresql@16 && brew services start postgresql@16
# Ubuntu
sudo apt install postgresql && sudo systemctl start postgresql
```

2. Install dependencies:
```bash
make install-local
```
This creates a `.venv/`, installs `any-llm-sdk[gateway,...]` from `ANYLLM_SDK_REF` (defaults to a known-good upstream ref) + router deps, sets up the `gateway` database, and copies config templates.

Before starting services, set at least one provider key in `.env` and ensure `ANYLLM_MASTER_KEY` is set.

3. Start both services (two terminals):
```bash
# Terminal 1: any-llm gateway on :8000
make run-gateway-local

# Terminal 2: cost router on :4000
make run-local
```

4. Create the default gateway user (required for router requests):
```bash
source .env
curl -s http://127.0.0.1:8000/v1/users \
  -H "Authorization: Bearer $ANYLLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"openclaw-local","alias":"openclaw-local"}'
```

## Verify

```bash
curl -s http://127.0.0.1:4000/health | jq
```

## OpenClaw Quick Connect (60 seconds)

ClawSwitch is designed to work with [OpenClaw](https://github.com/openclaw). If you just want to get connected fast:

1. Run `OPENAI_API_KEY=... ./scripts/install.sh` (or use `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, etc.).
2. Run `make print-openclaw`.
3. Paste those values into OpenClaw's provider settings.

In OpenClaw's provider settings, select **OpenAI-compatible** and enter:

| Setting | Value |
|---------|-------|
| Base URL | `http://127.0.0.1:4000/v1` |
| API Key | your `ROUTER_SHARED_KEY` from `.env` |
| Model | `claw-auto-cheap` |

Or run `make print-openclaw` to get a ready-to-paste JSON snippet:

```json
{
  "provider": "openai",
  "baseUrl": "http://127.0.0.1:4000/v1",
  "apiKey": "your-router-shared-key",
  "model": "claw-auto-cheap"
}
```

That's it. Every request from OpenClaw now gets automatically routed to the cheapest model that can handle it. You can check the `x-routed-model` response header to see which model was actually used.

Direct provider model ids are also supported. For Gemini, both `gemini:<model>` and `google:<model>` are accepted and normalized before forwarding to Any-LLM.

## Other Clients

ClawSwitch works with any OpenAI-compatible client — Cursor, Continue, aider, custom scripts, etc. Point it at `http://127.0.0.1:4000/v1` with model `claw-auto-cheap`.

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claw-auto-cheap",
    "messages": [{"role":"user","content":"Say hi in 10 words"}]
  }' | jq
```

If `ROUTER_SHARED_KEY` is set, add `-H "Authorization: Bearer $ROUTER_SHARED_KEY"`.

## How Routing Works

### Tiered routing (default)

1. Classify the request into `SIMPLE` / `MEDIUM` / `COMPLEX` / `REASONING` based on message length, keywords, tool use, and output token limits
2. Sort that tier's candidates by estimated cost (input + output)
3. Try cheapest first; on failure, fall back within the tier, then across tiers

### Flat routing

Skip classification, just pick the cheapest candidate across all models.

Configure both styles in `router/models.yml` — use `tiers:` for tiered, `candidates:` for flat. See `router/models.yml.example` for the full format.

## Customizing Your Routes

Everything is controlled in `router/models.yml`. You decide which models go in which tiers, what counts as "cheap", and how failures are handled. Changes are picked up on the next request — no restart needed.

### Choosing models per tier

Each tier has a list of candidates. Put the models you want in each one:

```yaml
models:
  claw-auto-cheap:
    tiers:
      SIMPLE:
        - model: gemini:gemini-2.5-flash     # cheap, fast
        - model: anthropic:claude-haiku-4-5
      MEDIUM:
        - model: gemini:gemini-2.5-flash
        - model: anthropic:claude-sonnet-4-6
      COMPLEX:
        - model: openai:gpt-5
        - model: anthropic:claude-sonnet-4-6
      REASONING:
        - model: openai:o3-mini
        - model: gemini:gemini-2.5-pro
```

You don't need all four tiers. If you only care about SIMPLE and COMPLEX, just define those two — ClawSwitch will map requests to the nearest available tier.

### Setting prices

Inline pricing on each candidate controls the cost sort order. Lower total cost = tried first.

```yaml
      SIMPLE:
        - model: gemini:gemini-2.5-flash
          input_price_per_million: 0.30
          output_price_per_million: 2.50
        - model: anthropic:claude-haiku-4-5
          input_price_per_million: 1.00
          output_price_per_million: 5.00
```

With these prices and a typical request, Gemini 2.5 Flash costs ~$0.002 vs Claude Haiku at ~$0.004, so Flash is tried first. If you'd rather prefer Haiku, just swap the prices or remove Gemini from the tier.

Pricing is also available from the gateway (`gateway/config.yml`) — inline prices in `models.yml` take priority.

### Controlling fallback

When a model fails (rate limit, outage, etc.), ClawSwitch tries the next candidate in the tier, then moves to other tiers. You control this:

```yaml
  claw-auto-cheap:
    fallback_tiers_on_failure: true           # try other tiers if all candidates in the selected tier fail
    tier_fallback_order: [COMPLEX, REASONING, SIMPLE]  # order to try after the selected tier
```

Set `fallback_tiers_on_failure: false` to only try models in the selected tier — no cross-tier fallback.

### Flat routing (no tiers)

If you don't want tier classification at all, use `candidates:` instead of `tiers:`. This just picks the cheapest model every time:

```yaml
  my-cheapest:
    description: "Always use the cheapest model"
    candidates:
      - model: gemini:gemini-2.5-flash
        input_price_per_million: 0.30
        output_price_per_million: 2.50
      - model: anthropic:claude-haiku-4-5
        input_price_per_million: 1.00
        output_price_per_million: 5.00
```

### Creating multiple aliases

You can define as many aliases as you want. Use different ones for different use cases:

```yaml
models:
  cheap:
    description: "Cheapest possible"
    candidates:
      - model: gemini:gemini-2.5-flash
        input_price_per_million: 0.30
        output_price_per_million: 2.50

  smart:
    description: "Best available"
    candidates:
      - model: anthropic:claude-sonnet-4-6
        input_price_per_million: 3.00
        output_price_per_million: 15.00
      - model: openai:gpt-5
        input_price_per_million: 2.00
        output_price_per_million: 8.00

  auto:
    description: "Tier-routed"
    tiers:
      SIMPLE:
        - model: gemini:gemini-2.5-flash
          input_price_per_million: 0.30
          output_price_per_million: 2.50
      COMPLEX:
        - model: openai:gpt-5
          input_price_per_million: 2.00
          output_price_per_million: 8.00
```

Then use `model: "cheap"`, `model: "smart"`, or `model: "auto"` in your requests.

### Direct model passthrough

You can also skip aliases entirely and send requests to a specific model:

```bash
curl ... -d '{"model": "gemini:gemini-2.5-flash", ...}'
```

Any `provider:model` string is forwarded directly to the gateway.

## Configuration Files

| File | Purpose |
|------|---------|
| `router/models.yml` | Aliases, tiers, candidates, inline pricing |
| `gateway/config.yml` | Provider API keys, gateway-level pricing |
| `.env` | API keys, router auth, ports |

## Response Headers

Every response includes routing metadata:
- `x-routed-model` — the actual model used
- `x-router-selected-tier` — tier the request was classified as
- `x-router-effective-tier` — nearest available tier used
- `x-router-routed-tier` — tier of the chosen model

## Built With

- [Any-LLM](https://github.com/mozilla-ai/any-llm) by Mozilla-AI — the local gateway that handles provider dispatch and API translation
