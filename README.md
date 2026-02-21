# ClawSwitch

This repo provides a local stack that routes OpenAI-compatible chat requests by capability tier first, then lowest estimated cost within that tier.

It uses:
- `any-llm-gateway` for provider credentials, usage logging, and model pricing storage
- a thin local `cost-router` service that classifies request tier and then picks the cheapest candidate in that tier (with fallback)

No BlockRun relay is involved. Traffic flow is:

`OpenClaw -> local cost-router -> local Any-LLM gateway -> provider endpoint`

## What You Get

- Local OpenAI-compatible endpoint at `http://127.0.0.1:4000/v1/chat/completions`
- Model aliases (for example `claw-auto-cheap`) with multiple provider candidates
- Tier-first routing (`SIMPLE`, `MEDIUM`, `COMPLEX`, `REASONING`)
- Cheapest-first selection within tier based on Any-LLM pricing table
- Automatic fallback across models (and optional cross-tier fallback)

## 60-Second Setup

```bash
OPENAI_API_KEY=your-key ./scripts/install.sh
```

That command:
- creates local config files if missing
- generates secure local keys
- starts all services
- prints the exact OpenClaw settings to copy/paste

If you use Anthropic/Mistral/Gemini instead, pass those keys instead.

## Quick Start

1. Copy templates:
```bash
cp .env.example .env
cp gateway/config.yml.example gateway/config.yml
cp router/models.yml.example router/models.yml
```

2. Edit `.env` and set:
- `ANYLLM_MASTER_KEY`
- provider keys you plan to use (for example `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)
- optional `ROUTER_SHARED_KEY` for local router auth

3. Edit `gateway/config.yml`:
- keep `master_key: "${ANYLLM_MASTER_KEY}"`
- keep only the providers you use
- update pricing values for the exact models you route to

4. Edit `router/models.yml`:
- define aliases as either:
  - tiered (`tiers:`) for capability-first routing
  - flat (`candidates:`) for pure cost-first routing
- model IDs must be `provider:model` format

5. Start the stack:
```bash
docker compose up -d --build
```

6. Verify:
```bash
curl -s http://127.0.0.1:4000/health | jq
curl -s http://127.0.0.1:4000/v1/models | jq
```

If `ROUTER_SHARED_KEY` is set, include:
```bash
-H "Authorization: Bearer <ROUTER_SHARED_KEY>"
```

Shortcut commands:
```bash
make install
make doctor
make print-openclaw
make logs
```

## OpenClaw Wiring

Point OpenClaw at:
- Base URL: `http://127.0.0.1:4000/v1`
- API key:
  - if `ROUTER_SHARED_KEY` is set, use that key
  - if `ROUTER_SHARED_KEY` is empty, any non-empty key is typically fine for OpenAI-compatible clients
- Model: one alias from `router/models.yml` (for example `claw-auto-cheap`)

To print these values from your current local setup:
```bash
./scripts/print-openclaw-settings.sh
```

## Example Request

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer ${ROUTER_SHARED_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claw-auto-cheap",
    "messages": [{"role":"user","content":"Say hi in 10 words"}]
  }' | jq
```

## Updating Pricing

Use the helper script:

```bash
ANYLLM_MASTER_KEY=your-key ./scripts/set-pricing.sh \
  openai:gpt-4.1-mini 0.40 1.60 \
  anthropic:claude-3-5-haiku-latest 0.80 4.00
```

Or update `gateway/config.yml` and restart.

## Routing Behavior

- Tiered alias flow:
  1. Classify request into `SIMPLE` / `MEDIUM` / `COMPLEX` / `REASONING`
  2. Sort that tier's candidates by estimated cost
  3. Try candidates in order, then optionally move to fallback tiers
- Flat alias flow:
  - Skip tiering and route purely by cheapest candidate

## Notes

- Cost estimate uses:
  - prompt tokens estimated from message text size
  - output tokens from `max_completion_tokens`/`max_tokens` (or alias default)
- If pricing is missing for a candidate model, it is still usable but ranked after priced candidates.
- Response headers include routing metadata:
  - `x-routed-model`
  - `x-router-selected-tier` (for tiered aliases)
  - `x-router-effective-tier` (nearest configured tier used)
  - `x-router-routed-tier` (tier of the actual chosen model)
