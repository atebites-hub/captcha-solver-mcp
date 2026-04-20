#!/usr/bin/env bash
# captcha-solver-mcp installer. Safe to re-run.
#
# 1. Sanity-check env vars
# 2. Verify CAPTCHA_SOLVER_MODEL is live on OpenRouter
# 3. Build + up the docker container (`docker compose up -d --build`)
# 4. Wait for the FastAPI /health endpoint
# 5. Merge mcp-servers.yaml into ~/.hermes/config.yaml (Python yaml-merge)
# 6. Symlink SKILL.md into ~/.hermes/skills/mcp/captcha-solver/
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CONFIG="$HERMES_HOME/config.yaml"
ENVFILE="$HERMES_HOME/.env"
PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python3"

if [[ ! -f "$CONFIG" ]]; then
  echo "FATAL: $CONFIG not found. Install Hermes first." >&2
  exit 1
fi

# ── Env sanity ─────────────────────────────────────────────────────────
set -a
# shellcheck disable=SC1090
source "$ENVFILE"
set +a

: "${OPENROUTER_API_KEY:?must set OPENROUTER_API_KEY in $ENVFILE (https://openrouter.ai/settings/keys)}"
: "${CAPTCHA_SOLVER_MODEL:?must set CAPTCHA_SOLVER_MODEL in $ENVFILE (e.g. google/gemma-4-31b-it)}"

# ── Verify model is live on OpenRouter ─────────────────────────────────
echo "[captcha-solver] verifying model '$CAPTCHA_SOLVER_MODEL' on OpenRouter..."
if command -v jq >/dev/null; then
  if ! curl -sfH "Authorization: Bearer $OPENROUTER_API_KEY" \
      "https://openrouter.ai/api/v1/models" \
      | jq -e ".data[] | select(.id == \"$CAPTCHA_SOLVER_MODEL\")" >/dev/null; then
    echo "FATAL: model '$CAPTCHA_SOLVER_MODEL' not available on OpenRouter." >&2
    echo "Check https://openrouter.ai/models and update CAPTCHA_SOLVER_MODEL in $ENVFILE." >&2
    exit 1
  fi
else
  echo "[captcha-solver] jq not installed; skipping model availability check"
fi

# ── Docker build + up ──────────────────────────────────────────────────
echo "[captcha-solver] docker compose up -d --build"
cd "$HERE"
docker compose up -d --build

# ── Wait for /health ───────────────────────────────────────────────────
echo "[captcha-solver] waiting for /health..."
for i in $(seq 1 30); do
  if curl -sf --max-time 3 http://127.0.0.1:8899/health >/dev/null 2>&1; then
    echo "[captcha-solver] healthy"
    break
  fi
  sleep 2
  if [[ $i -eq 30 ]]; then
    echo "FATAL: /health never came up after 60s. Check: docker logs captcha-solver" >&2
    exit 1
  fi
done

# ── Merge MCP entry into config.yaml ───────────────────────────────────
echo "[captcha-solver] merging MCP entry into $CONFIG"
"$PYTHON" - "$CONFIG" "$HERE/mcp-servers.yaml" <<'PY'
import sys, yaml, shutil
cfg_path, spec_path = sys.argv[1], sys.argv[2]
with open(cfg_path) as f:
    cfg = yaml.safe_load(f) or {}
with open(spec_path) as f:
    spec = yaml.safe_load(f) or {}
shutil.copy(cfg_path, cfg_path + ".bak.captcha-solver-install")
servers = cfg.setdefault("mcp_servers", {})
for name, entry in spec.items():
    if servers.get(name) == entry:
        print(f"  = {name} (already present)")
    else:
        servers[name] = entry
        print(f"  + {name}")
with open(cfg_path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
PY

# ── Symlink SKILL.md ───────────────────────────────────────────────────
SKILL_DIR="$HERMES_HOME/skills/mcp/captcha-solver"
mkdir -p "$SKILL_DIR"
ln -sfn "$HERE/skills/captcha-solver/SKILL.md" "$SKILL_DIR/SKILL.md"
echo "[captcha-solver] SKILL.md symlinked into $SKILL_DIR"

echo "[captcha-solver] installed. To activate, restart the gateway:"
echo "    systemctl --user restart hermes-gateway.service"
