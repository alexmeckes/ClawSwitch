#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

need_cmd docker
need_cmd curl

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose (v2) is required."
  exit 1
fi

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48
  fi
}

env_get() {
  local key="$1"
  local file="$2"
  local line
  line="$(grep -E "^${key}=" "${file}" 2>/dev/null | head -n 1 || true)"
  echo "${line#*=}"
}

discover_first_alias() {
  local models_file="$1"
  awk '
    /^models:/ { in_models=1; next }
    in_models && $0 ~ /^  [A-Za-z0-9._-]+:$/ {
      alias=$1
      sub(":", "", alias)
      print alias
      exit
    }
  ' "${models_file}"
}

env_upsert() {
  local key="$1"
  local value="$2"
  local file="$3"
  local tmp_file
  tmp_file="$(mktemp)"

  awk -v key="${key}" -v value="${value}" '
    BEGIN { replaced=0 }
    $0 ~ ("^" key "=") {
      if (!replaced) {
        print key "=" value
        replaced=1
      } else {
        print $0
      }
      next
    }
    { print $0 }
    END {
      if (!replaced) {
        print key "=" value
      }
    }
  ' "${file}" > "${tmp_file}"

  mv "${tmp_file}" "${file}"
}

echo "[1/6] Preparing local config files..."
if [ ! -f ".env" ]; then
  cp .env.example .env
fi
if [ ! -f "gateway/config.yml" ]; then
  cp gateway/config.yml.example gateway/config.yml
fi
if [ ! -f "router/models.yml" ]; then
  cp router/models.yml.example router/models.yml
fi

echo "[2/6] Applying environment values..."
for key_var in OPENAI_API_KEY ANTHROPIC_API_KEY MISTRAL_API_KEY GEMINI_API_KEY ROUTER_SHARED_KEY; do
  value="${!key_var:-}"
  if [ -n "${value}" ]; then
    env_upsert "${key_var}" "${value}" ".env"
  fi
done

master_key="$(env_get "ANYLLM_MASTER_KEY" ".env")"
if [ -z "${master_key}" ] || [ "${master_key}" = "replace-with-long-random-string" ]; then
  master_key="$(generate_secret)"
  env_upsert "ANYLLM_MASTER_KEY" "${master_key}" ".env"
fi

router_key="$(env_get "ROUTER_SHARED_KEY" ".env")"
if [ -z "${router_key}" ] && [ "${GENERATE_ROUTER_KEY:-1}" = "1" ]; then
  router_key="$(generate_secret)"
  env_upsert "ROUTER_SHARED_KEY" "${router_key}" ".env"
fi

echo "[3/7] Checking provider keys..."
has_provider_key=0
for key_var in OPENAI_API_KEY ANTHROPIC_API_KEY MISTRAL_API_KEY GEMINI_API_KEY; do
  value="$(env_get "${key_var}" ".env")"
  if [ -n "${value}" ]; then
    has_provider_key=1
    break
  fi
done

if [ "${has_provider_key}" -eq 0 ]; then
  echo ""
  echo "No provider API keys found in .env."
  echo "Set at least one provider key, then re-run:"
  echo "  OPENAI_API_KEY=... ./scripts/install.sh"
  echo ""
  exit 1
fi

echo "[4/7] Starting stack..."
docker compose up -d --build

echo "[5/7] Waiting for health checks..."
deadline=$((SECONDS + 120))
while [ "${SECONDS}" -lt "${deadline}" ]; do
  if curl -fsS "http://127.0.0.1:4000/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! curl -fsS "http://127.0.0.1:4000/health" >/dev/null 2>&1; then
  echo "Router did not become healthy in time."
  echo "Recent logs:"
  docker compose logs --tail=120 anyllm cost-router
  exit 1
fi

if [ "${SKIP_CHAT_SMOKE_TEST:-0}" != "1" ]; then
  echo "[6/7] Running chat smoke test..."
  router_key="$(env_get "ROUTER_SHARED_KEY" ".env")"
  default_model="$(env_get "OPENCLAW_MODEL" ".env")"
  if [ -z "${default_model}" ] && [ -f "router/models.yml" ]; then
    default_model="$(discover_first_alias "router/models.yml")"
  fi
  if [ -z "${default_model}" ]; then
    default_model="claw-auto-cheap"
  fi

  smoke_payload="$(cat <<EOF
{"model":"${default_model}","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":8}
EOF
)"

  smoke_output_file="$(mktemp)"
  if [ -n "${router_key}" ]; then
    smoke_status="$(
      curl -sS -o "${smoke_output_file}" -w "%{http_code}" "http://127.0.0.1:4000/v1/chat/completions" \
        -H "Authorization: Bearer ${router_key}" \
        -H "Content-Type: application/json" \
        -d "${smoke_payload}" || true
    )"
  else
    smoke_status="$(
      curl -sS -o "${smoke_output_file}" -w "%{http_code}" "http://127.0.0.1:4000/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "${smoke_payload}" || true
    )"
  fi

  if [[ ! "${smoke_status}" =~ ^2 ]]; then
    echo "Chat smoke test failed (HTTP ${smoke_status})."
    echo "Response:"
    sed -n '1,160p' "${smoke_output_file}"
    rm -f "${smoke_output_file}"
    echo ""
    echo "Recent logs:"
    docker compose logs --tail=120 anyllm cost-router
    exit 1
  fi
  rm -f "${smoke_output_file}"
else
  echo "[6/7] Chat smoke test skipped (SKIP_CHAT_SMOKE_TEST=1)."
fi

echo "[7/7] Ready."
echo ""
"${ROOT_DIR}/scripts/print-openclaw-settings.sh"
