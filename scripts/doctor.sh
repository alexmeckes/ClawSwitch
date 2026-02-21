#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

env_get() {
  local key="$1"
  local file="$2"
  local line
  line="$(grep -E "^${key}=" "${file}" 2>/dev/null | head -n 1 || true)"
  echo "${line#*=}"
}

router_key=""
if [ -f ".env" ]; then
  router_key="$(env_get "ROUTER_SHARED_KEY" ".env")"
fi

echo "Containers:"
docker compose ps

echo ""
echo "Router health:"
curl -fsS "http://127.0.0.1:4000/health"
echo ""
echo ""
echo "Any-LLM health:"
curl -fsS "http://127.0.0.1:8000/health"
echo ""
echo ""
echo "Router models:"
if [ -n "${router_key}" ]; then
  curl -fsS "http://127.0.0.1:4000/v1/models" -H "Authorization: Bearer ${router_key}"
else
  curl -fsS "http://127.0.0.1:4000/v1/models"
fi
echo ""
