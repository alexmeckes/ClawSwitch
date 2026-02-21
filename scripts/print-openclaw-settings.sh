#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
MODELS_FILE="${ROOT_DIR}/router/models.yml"

env_get() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | head -n 1 || true)"
  echo "${line#*=}"
}

discover_first_alias() {
  awk '
    /^models:/ { in_models=1; next }
    in_models && $0 ~ /^  [A-Za-z0-9._-]+:$/ {
      alias=$1
      sub(":", "", alias)
      print alias
      exit
    }
  ' "${MODELS_FILE}"
}

router_key="$(env_get "ROUTER_SHARED_KEY")"
if [ -z "${router_key}" ]; then
  router_key="local-openclaw"
fi

default_model="$(env_get "OPENCLAW_MODEL")"
if [ -z "${default_model}" ] && [ -f "${MODELS_FILE}" ]; then
  default_model="$(discover_first_alias)"
fi
if [ -z "${default_model}" ]; then
  default_model="claw-auto-cheap"
fi

base_url="http://127.0.0.1:4000/v1"

echo "OpenClaw settings:"
echo "  Base URL: ${base_url}"
echo "  API Key: ${router_key}"
echo "  Model: ${default_model}"
echo ""
echo "Paste these values into your OpenClaw provider setup."
echo ""
echo "JSON snippet:"
cat <<EOF
{
  "provider": "openai",
  "baseUrl": "${base_url}",
  "apiKey": "${router_key}",
  "model": "${default_model}"
}
EOF
