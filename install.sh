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

: "${GLM_API_KEY:?must set GLM_API_KEY in $ENVFILE (your Z.AI Coding plan key)}"
: "${CAPTCHA_SOLVER_MODEL:=glm-5v-turbo}"
# Must be a GLM model — captcha-solver only supports Z.AI Coding-plan vision
case "$CAPTCHA_SOLVER_MODEL" in
  glm-*) ;;
  *) echo "FATAL: CAPTCHA_SOLVER_MODEL must start with 'glm-' (got '$CAPTCHA_SOLVER_MODEL'). OpenRouter support was removed — this solver is Z.AI-only now." >&2; exit 1 ;;
esac

# ── Smoke-test the Coding-plan Vision Understanding endpoint ───────────
echo "[captcha-solver] smoke-testing Z.AI /paas/v4 + X-Title unlock..."
RESP=$(curl -sf --max-time 15 -X POST "https://api.z.ai/api/paas/v4/chat/completions" \
  -H "Authorization: Bearer $GLM_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Title: 4.5V MCP Local" \
  -d "{\"model\":\"$CAPTCHA_SOLVER_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":5,\"thinking\":{\"type\":\"disabled\"}}" 2>&1) || {
    echo "FATAL: smoke test of Z.AI /paas/v4 with X-Title hack failed." >&2
    echo "Response: $RESP" >&2
    echo "Check GLM_API_KEY is a valid Z.AI Coding plan key." >&2
    exit 1
  }

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
