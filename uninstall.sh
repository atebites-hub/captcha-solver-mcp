#!/usr/bin/env bash
# Remove the captcha-solver MCP + container + SKILL symlink.
# Leaves the _plugins/ source tree in place for future re-install.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CONFIG="$HERMES_HOME/config.yaml"
PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python3"

# ── Docker down ────────────────────────────────────────────────────────
echo "[captcha-solver] docker compose down -v"
cd "$HERE"
docker compose down -v 2>/dev/null || true
docker image rm captcha-solver-mcp:latest 2>/dev/null || true

# ── Remove MCP entry from config ───────────────────────────────────────
if [[ -f "$CONFIG" ]]; then
  echo "[captcha-solver] removing MCP entry from $CONFIG"
  "$PYTHON" - "$CONFIG" <<'PY'
import sys, yaml, shutil
cfg_path = sys.argv[1]
with open(cfg_path) as f:
    cfg = yaml.safe_load(f) or {}
shutil.copy(cfg_path, cfg_path + ".bak.captcha-solver-uninstall")
servers = cfg.get("mcp_servers") or {}
if "captcha-solver" in servers:
    del servers["captcha-solver"]
    print("  - captcha-solver")
if not servers:
    cfg.pop("mcp_servers", None)
with open(cfg_path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
PY
fi

# ── Remove SKILL symlink ───────────────────────────────────────────────
SKILL_DIR="$HERMES_HOME/skills/mcp/captcha-solver"
if [[ -L "$SKILL_DIR/SKILL.md" ]]; then
  rm -f "$SKILL_DIR/SKILL.md"
  rmdir "$SKILL_DIR" 2>/dev/null || true
  echo "[captcha-solver] SKILL.md symlink removed"
fi

echo "[captcha-solver] uninstalled. Restart gateway to clean up MCP subprocesses:"
echo "    systemctl --user restart hermes-gateway.service"
