#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 3 ] || [ $(( $# % 3 )) -ne 0 ]; then
  echo "Usage:"
  echo "  ANYLLM_MASTER_KEY=... $0 <model_key> <input_price_per_million> <output_price_per_million> [repeat...]"
  echo ""
  echo "Example:"
  echo "  ANYLLM_MASTER_KEY=secret $0 openai:gpt-4.1-mini 0.40 1.60 anthropic:claude-3-5-haiku-latest 0.80 4.00"
  exit 1
fi

if [ -z "${ANYLLM_MASTER_KEY:-}" ]; then
  echo "ANYLLM_MASTER_KEY is required."
  exit 1
fi

ANYLLM_BASE_URL="${ANYLLM_BASE_URL:-http://localhost:8000}"

while [ $# -gt 0 ]; do
  model_key="$1"
  input_price="$2"
  output_price="$3"
  shift 3

  echo "Setting pricing for ${model_key}..."
  curl -fsS -X POST "${ANYLLM_BASE_URL}/v1/pricing" \
    -H "Authorization: Bearer ${ANYLLM_MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"model_key\":\"${model_key}\",\"input_price_per_million\":${input_price},\"output_price_per_million\":${output_price}}" \
    >/dev/null
done

echo "Done."
