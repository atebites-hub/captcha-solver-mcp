# captcha-solver-mcp

CAPTCHA tile classifier exposed as a Hermes MCP. The plugin itself does not
drive a browser — it just takes per-tile images and returns which indices
contain the target object. Hermes drives its own Camofox browser end-to-end
using `browser_navigate`, `browser_snapshot`, `browser_screenshot_element`,
and `browser_click`.

## Why classifier-only (v0.2 rewrite)

The original design ran its own Camoufox inside the container and handed
back tokens. Two problems killed that:

1. **Fingerprint mismatch.** Google's "sorry" page rendered differently for
   the solver's browser than for Hermes's, because they had different TLS
   fingerprints and cookies. Tokens earned in the solver's session were
   rejected when Hermes replayed them.
2. **Session handoff is fragile.** Even with matched UAs and cookies, any
   header drift caused silent rejection.

The v0.2 design moves the browser into Hermes (now running on Camofox
via the native provider — see the Hermes Buildbook). The solver shrinks
to one responsibility: **look at a tile, say yes or no**.

## Tool surface

One MCP tool: `captcha-solver:solve_tiles`.

```
solve_tiles(
    target: str,                        # "crosswalks", "buses", ...
    tile_images: [base64-png],          # order matters
    instruction_image: base64-png       # optional override
)
→ {
    match_indices: [int],               # 0-based into tile_images
    target: str,                        # the target we actually classified
    model_used: str,
    per_tile_latency_ms: [int],
    total_elapsed_ms: int,
}
```

## Architecture

```
Hermes gateway (admin, systemd user)
  │
  │ (1) browser_navigate → Camofox (127.0.0.1:9377)
  │ (2) browser_snapshot → ref list
  │ (3) browser_screenshot_element(ref) per tile → PNGs
  │ (4) captcha-solver:solve_tiles(target, tile_images)
  ▼
stdio MCP shim  (src/mcp_shim.py)
  │ HTTP POST to 127.0.0.1:8899/solve_tiles
  ▼
FastAPI tile classifier  (src/server.py)
  │
  ▼
VLMClient → Z.AI /paas/v4/chat/completions
           with X-Title: 4.5V MCP Local   (Coding plan Vision pool)
           and thinking:{type:"enabled"}
```

## The Z.AI unlock

Direct `/paas/v4/chat/completions` with `glm-5v-turbo` normally returns
`1113 Insufficient balance` for Coding-plan-only keys. `@z_ai/mcp-server`
succeeds because it sends:

```
X-Title: 4.5V MCP Local
Accept-Language: en-US,en
```

That routes the call through the **Coding plan's Vision Understanding
pool** (shared 5-hour prompt budget) instead of per-token billing.
`vlm_client.py` adds those headers on every call. Details in the
[[Z.AI Vision Hack]] buildbook page.

## Environment

| Variable | Purpose |
|---|---|
| `GLM_API_KEY` | Z.AI Coding-plan key (shared with the rest of Hermes's GLM config) |
| `CAPTCHA_SOLVER_MODEL` | VLM id — default `glm-5v-turbo`; `glm-4.6v` also works |

No OpenRouter, no fallback chain — one subscription, one billing path. If
Z.AI goes down, the solver errors; that's explicit and acceptable.

## Install (standalone disaster-recovery path)

```sh
git clone https://github.com/atebites-hub/captcha-solver-mcp.git \
  ~/.hermes/plugins/captcha-solver-mcp

cat >> ~/.hermes/.env <<EOF
GLM_API_KEY=<your Z.AI Coding plan key>
CAPTCHA_SOLVER_MODEL=glm-5v-turbo
EOF
chmod 600 ~/.hermes/.env

~/.hermes/plugins/captcha-solver-mcp/install.sh
systemctl --user restart hermes-gateway.service
```

Installer is idempotent. It builds the Docker image (~200 MB — Python +
FastAPI only, no Camoufox), waits for `/health`, merges the MCP entry
into `~/.hermes/config.yaml`, and symlinks SKILL.md into the Hermes
skills tree for auto-discovery.

## Uninstall

```sh
~/.hermes/plugins/captcha-solver-mcp/uninstall.sh
systemctl --user restart hermes-gateway.service
```

## Manual test

```sh
# /health
curl -s http://127.0.0.1:8899/health

# solve_tiles (need a real PNG; use cat file.png | base64 -w0)
curl -sX POST http://127.0.0.1:8899/solve_tiles \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg b64 "$(base64 -w0 tile.png)" '{target:"car",tile_images:[$b64]}')"
```

## Known limitations

- **Only tile-classification CAPTCHAs** — reCAPTCHA v2 image grids, hCaptcha
  image grids. Spatial-reasoning (drag, rotate, point) variants are out of
  scope for this classifier; the agent should surface those to the user.
- **Accuracy ceiling is whatever glm-5v-turbo can do** with thinking on.
  Measured ~85-90% on reCAPTCHA v2 crosswalks; drops on Enterprise hCaptcha.
- **No per-hour budget cap** — if the agent loops, it just eats the
  5-hour prompt pool faster. Mitigate by keeping `max_turns` reasonable.

## Licensing

MIT. Runtime deps (FastAPI, httpx, openai, mcp, Pillow-not-used-anymore):
all permissive.
