# captcha-solver-mcp

Containerized CAPTCHA solver bundled as a Hermes plugin. Handles:

- **reCAPTCHA v2** (vision, image-grid + dynamic 4×4 hard mode)
- **reCAPTCHA v3** (invisible token harvest via `grecaptcha.execute`)
- **hCaptcha** (image grid, click-on-X, drag-to-match — custom VLM solver)
- **Cloudflare Turnstile** (checkbox + auto-pass)
- **Cloudflare Managed Challenge** (returns `cf_clearance` cookie + UA)

Exposes one MCP tool: `captcha-solver:solve(type, site_url, site_key, timeout_s)`.

## Architecture

```
Hermes gateway
  │
  ▼
stdio MCP shim  (src/mcp_shim.py, host-side)
  │ HTTP POST to 127.0.0.1:8899
  ▼
FastAPI dispatcher  (src/server.py, inside Docker)
  ├─ recaptcha_v2   — screenshot grid, per-tile VLM classify, click, verify
  ├─ recaptcha_v3   — wait for grecaptcha.execute in main-world, harvest token
  ├─ hcaptcha       — screenshot challenge iframe, VLM action plan (click
  │                   points / click tiles / drag), execute, submit, loop
  ├─ turnstile      — poll cf-turnstile-response; click iframe checkbox if visible
  └─ cf_managed     — navigate interstitial, poll cf_clearance cookie, return
                      {cookies, user_agent} (agent must sync UA before reuse)
```

All solvers share a Camoufox browser session (`src/browser.py`) with stealth
defaults: `main_world_eval=True` (reach page-script globals), `block_webgl=False`
(reCAPTCHA's fingerprinting probe stalls without WebGL), `os=linux`.

VLM calls go through `src/vlm_client.py`, which **auto-routes per model id**:
- GLM models (`glm-*`) → Z.AI `/paas/v4` with the `X-Title: 4.5V MCP Local`
  header, billed against the **Coding plan's Vision Understanding pool**
  (zero incremental cost)
- Other models → OpenRouter `/api/v1`

## Environment

| Variable | Purpose |
|---|---|
| `GLM_API_KEY` | Z.AI Coding plan key (required if using GLM models — shared with main Hermes config) |
| `OPENROUTER_API_KEY` | OpenRouter key (required if not using GLM models) |
| `CAPTCHA_SOLVER_MODEL` | Primary VLM id (default: `glm-5v-turbo`) |
| `CAPTCHA_SOLVER_MODEL_FALLBACK` | Fallback on 429/5xx (default: `glm-4.6v`) |

### Model options

| Model | Backend | Billing | Best for |
|---|---|---|---|
| **`glm-5v-turbo`** | Z.AI `/paas/v4` | Coding plan pool | **Recommended**: strong vision, handles v2 4×4 hard mode |
| `glm-4.6v` | Z.AI `/paas/v4` | Coding plan pool | Good fallback; slightly older |
| `google/gemma-4-31b-it` | OpenRouter free tier | 50/1000 req/day | Budget option; weaker on hard challenges |
| `qwen/qwen3-vl-32b-instruct` | OpenRouter paid | ~$0.005/solve | Strong general VLM without needing a Z.AI key |
| `bytedance/ui-tars-1.5-7b` | OpenRouter paid | ~$0.004/solve | Computer-use-tuned; useful for action-planning hCaptcha variants |

### The Z.AI unlock explained

Direct `/paas/v4/chat/completions` with `glm-5v-turbo` normally returns
`1113 Insufficient balance` for Coding-plan-only keys. The Z.AI MCP server
(`@z_ai/mcp-server`) succeeds on the same endpoint because it sends:

```
X-Title: 4.5V MCP Local
Accept-Language: en-US,en
```

With that header, the same call routes through the **Vision Understanding**
tier of the Coding plan — the same 5-hour prompt pool the rest of your Z.AI
stack uses. `vlm_client.py` adds this header automatically for any model id
starting with `glm-`.

## Install (standalone)

```sh
git clone https://github.com/atebites-hub/captcha-solver-mcp.git \
  ~/.hermes/plugins/captcha-solver-mcp

cat >> ~/.hermes/.env <<EOF
# Either GLM key (recommended — no extra billing) OR OpenRouter key
GLM_API_KEY=<your Z.AI Coding plan key>
CAPTCHA_SOLVER_MODEL=glm-5v-turbo
CAPTCHA_SOLVER_MODEL_FALLBACK=glm-4.6v
EOF
chmod 600 ~/.hermes/.env

~/.hermes/plugins/captcha-solver-mcp/install.sh
systemctl --user restart hermes-gateway.service
```

Installer is idempotent — safe to re-run. Builds Docker image, waits for
`/health`, merges MCP entry into `~/.hermes/config.yaml`, symlinks SKILL.md
into Hermes's skills tree for auto-discovery.

## Uninstall

```sh
~/.hermes/plugins/captcha-solver-mcp/uninstall.sh
systemctl --user restart hermes-gateway.service
```

## Testing

```sh
~/.hermes/plugins/captcha-solver-mcp/test.sh
```

Runs smoke tests for each CAPTCHA type against public demo sites.

Manual single test:

```sh
curl -X POST http://127.0.0.1:8899/solve \
  -H 'Content-Type: application/json' \
  -d '{"type":"recaptcha_v2","site_url":"https://2captcha.com/demo/recaptcha-v2","site_key":"6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u","timeout_s":270}'
```

## Verification status (2026-04-20)

| Type | Target | Result |
|---|---|---|
| reCAPTCHA v3 | 2captcha.com | ✅ 5/5 trials, ~8s each |
| cf_managed | nowsecure.nl | ✅ 3.7s, `cf_clearance` cookie + UA |
| Turnstile fast-path | demo.turnstile.workers.dev | ✅ 5s |
| hCaptcha | democaptcha.com | ✅ works (multi-page handled) |
| reCAPTCHA v2 (glm-5v-turbo) | 2captcha.com | ✅ first-trial solve in 40s |

## Known limitations

- **IP cohesion**: `cf_clearance` cookies bind the solver container's egress IP.
  Solver and agent must share egress (same VPS). Running solver on a different
  host breaks strict-fingerprint sites (Cloudflare especially).
- **UA sync (cf_managed)**: agent MUST use the returned `user_agent` before
  injecting `cf_clearance`, or CF revokes the cookie.
- **Model accuracy**: free Gemma-4-31B struggles on 4×4 hard mode. Upgrade to
  `glm-5v-turbo` (Coding plan, no extra cost) or `qwen/qwen3-vl-32b` (paid,
  $0.005/solve) for reliable v2 solves.
- **No per-hour spend cap** in v0.1.0. A runaway agent could drain OpenRouter
  credit. Future work: add a budget guard in `vlm_client.py`.

## Licensing

**MIT.** All runtime dependencies are MIT / Apache 2.0 / MPL-2.0 / source-
available. No copyleft contamination — hCaptcha solving is in-house, not via
`hcaptcha-challenger`. See `src/LICENSES/NOTICE` for full attribution.

## References

- [aydinnyunus/ai-captcha-bypass](https://github.com/aydinnyunus/ai-captcha-bypass) — reCAPTCHA v2 flow reference
- [daijro/camoufox](https://github.com/daijro/camoufox) — stealth Firefox
- [@z_ai/mcp-server](https://www.npmjs.com/package/@z_ai/mcp-server) — source of the X-Title Vision Understanding unlock
- [OpenRouter](https://openrouter.ai/) — pluggable non-GLM VLM endpoint
