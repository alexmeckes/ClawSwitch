#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== ClawSwitch local install (no Docker) ==="
echo ""

# ── [1/5] Create Python venv ─────────────────────────────────────────
VENV="$ROOT/.venv"
if [ ! -d "$VENV" ]; then
  echo "[1/5] Creating Python venv in .venv/ ..."
  python3 -m venv "$VENV"
else
  echo "[1/5] .venv/ already exists, reusing."
fi

# ── [2/5] Install dependencies ───────────────────────────────────────
echo "[2/5] Installing Python dependencies ..."
"$VENV/bin/pip" install --quiet --upgrade pip

# any-llm-gateway with provider extras
"$VENV/bin/pip" install --quiet \
  'any-llm-sdk[gateway,openai,anthropic,mistral,gemini]'

# cost-router dependencies
"$VENV/bin/pip" install --quiet -r "$ROOT/router/requirements.txt"

# ── [3/5] Check Postgres ─────────────────────────────────────────────
echo "[3/5] Checking Postgres ..."
if command -v pg_isready &>/dev/null && pg_isready -q 2>/dev/null; then
  echo "  ✓ Postgres is running"
else
  echo "  ⚠ Postgres does not appear to be running."
  echo "  Install and start it, e.g.:"
  echo "    macOS:  brew install postgresql@16 && brew services start postgresql@16"
  echo "    Ubuntu: sudo apt install postgresql && sudo systemctl start postgresql"
  echo ""
fi

# Create gateway DB if it doesn't exist (ignores errors if it already exists)
if command -v createdb &>/dev/null; then
  createdb gateway 2>/dev/null && echo "  ✓ Created database 'gateway'" \
    || echo "  ✓ Database 'gateway' already exists (or check permissions)"
fi

# ── [4/5] Copy config files from templates if missing ────────────────
echo "[4/5] Setting up config files ..."

if [ ! -f "$ROOT/router/models.yml" ]; then
  cp "$ROOT/router/models.yml.example" "$ROOT/router/models.yml"
  echo "  ✓ Copied router/models.yml.example → router/models.yml"
else
  echo "  ✓ router/models.yml already exists"
fi

if [ ! -f "$ROOT/gateway/config.yml" ]; then
  # Create local config pointing at localhost Postgres
  sed 's|postgresql://gateway:gateway@postgres:5432/gateway|postgresql://localhost/gateway|' \
    "$ROOT/gateway/config.yml.example" > "$ROOT/gateway/config.yml"
  echo "  ✓ Created gateway/config.yml (database_url → localhost)"
  echo "  ⚠ Edit gateway/config.yml if your Postgres uses different credentials."
else
  echo "  ✓ gateway/config.yml already exists"
fi

# ── [5/5] Print next steps ───────────────────────────────────────────
echo "[5/5] Done!"
echo ""
echo "Set your provider API keys, then start both services:"
echo ""
echo "  export OPENAI_API_KEY=sk-..."
echo "  # export ANTHROPIC_API_KEY=sk-ant-..."
echo ""
echo "  make run-gateway-local   # terminal 1: any-llm gateway on :8000"
echo "  make run-local           # terminal 2: cost router on :4000"
echo ""
echo "After the gateway is running, create the default user:"
echo ""
echo "  curl -s http://127.0.0.1:8000/v1/users -H 'Content-Type: application/json' -d '{\"user_id\":\"openclaw-local\",\"name\":\"openclaw-local\"}'"
echo ""
echo "Then test with:"
echo ""
echo "  curl -s http://127.0.0.1:4000/health | jq"
echo ""
