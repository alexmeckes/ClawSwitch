# ClawSwitch

A local LLM cost router that runs entirely on your machine. No hosted service, no third-party proxy, no data leaving your network except directly to the LLM providers you choose.

You call one endpoint with `model: "claw-auto-cheap"`. ClawSwitch classifies your request by complexity (`SIMPLE` / `MEDIUM` / `COMPLEX` / `REASONING`), picks the cheapest model in that tier, and falls back to others if it fails. Same results, lower cost, full privacy.

**Everything runs locally.** Your API keys stay on your machine. Your prompts go straight from your machine to the provider — OpenAI, Anthropic, Mistral, etc. — with nothing in between that you don't control.

```
Your app → ClawSwitch (:4000) → Any-LLM gateway (:8000) → provider API
             (your machine)         (your machine)
```

## Install

There are two ways to run ClawSwitch. Both give you the same functionality.

### Option A: Docker (recommended)

Requires: Docker + Docker Compose

```bash
OPENAI_API_KEY=your-key ./scripts/install.sh
```

That's it. This creates config files, generates local keys, starts all services, runs a smoke test, and prints your connection settings. Pass `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`, or `GEMINI_API_KEY` instead (or in addition) for other providers.

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
This creates a `.venv/`, installs `any-llm-sdk[gateway]` + router deps, sets up the `gateway` database, and copies config templates.

3. Start both services (two terminals):
```bash
# Terminal 1: any-llm gateway on :8000
make run-gateway-local

# Terminal 2: cost router on :4000
make run-local
```

## Verify

```bash
curl -s http://127.0.0.1:4000/health | jq
```

## Usage

ClawSwitch exposes a standard OpenAI-compatible API at `http://127.0.0.1:4000/v1`.

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claw-auto-cheap",
    "messages": [{"role":"user","content":"Say hi in 10 words"}]
  }' | jq
```

If `ROUTER_SHARED_KEY` is set, add `-H "Authorization: Bearer $ROUTER_SHARED_KEY"`.

Point any OpenAI-compatible client (OpenClaw, Cursor, etc.) at:
- **Base URL**: `http://127.0.0.1:4000/v1`
- **API key**: your `ROUTER_SHARED_KEY` (or any non-empty string if unset)
- **Model**: `claw-auto-cheap` (or any alias from `router/models.yml`)

Run `make print-openclaw` to see these values for your current setup.

## How Routing Works

### Tiered routing (default)

1. Classify the request into `SIMPLE` / `MEDIUM` / `COMPLEX` / `REASONING` based on message length, keywords, tool use, and output token limits
2. Sort that tier's candidates by estimated cost (input + output)
3. Try cheapest first; on failure, fall back within the tier, then across tiers

### Flat routing

Skip classification, just pick the cheapest candidate across all models.

Configure both styles in `router/models.yml` — use `tiers:` for tiered, `candidates:` for flat. See `router/models.yml.example` for the full format.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | Provider API keys, router auth, ports |
| `gateway/config.yml` | Any-LLM provider credentials, model pricing |
| `router/models.yml` | Model aliases, tier definitions, candidates |

### Updating pricing

```bash
ANYLLM_MASTER_KEY=your-key ./scripts/set-pricing.sh \
  openai:gpt-4.1-mini 0.40 1.60 \
  anthropic:claude-3-5-haiku-latest 0.80 4.00
```

Or edit `gateway/config.yml` directly and restart.

## Response Headers

Every response includes routing metadata:
- `x-routed-model` — the actual model used
- `x-router-selected-tier` — tier the request was classified as
- `x-router-effective-tier` — nearest available tier used
- `x-router-routed-tier` — tier of the chosen model
