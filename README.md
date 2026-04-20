# captcha-solver-mcp

A containerized CAPTCHA solver wrapped as a Hermes Agent MCP plugin. Uses
stealth-Firefox (Camoufox) + any OpenAI-compatible vision model (defaults to
OpenRouter's free `google/gemma-4-31b-it`) to solve:

- **reCAPTCHA v2** (image grid, including dynamic 4×4 hard mode)
- **reCAPTCHA v3** (invisible token harvest via `grecaptcha.execute`)
- **hCaptcha** (image grid, click-on-X, drag-to-match — custom VLM solver)
- **Cloudflare Turnstile** (checkbox + auto-pass)
- **Cloudflare Managed Challenge** (returns `cf_clearance` cookie + UA)

Exposes one MCP tool: `captcha-solver:solve(type, site_url, site_key, timeout_s)`.

## Quick start — disaster recovery deploy

You need a host with Docker + an OpenRouter API key (free at
https://openrouter.ai/settings/keys).

```sh
# 1. Clone
git clone https://github.com/atebites-hub/captcha-solver-mcp.git ~/.hermes/plugins/captcha-solver-mcp

# 2. Add OpenRouter key to the Hermes env
cat >> ~/.hermes/.env <<EOF
OPENROUTER_API_KEY=sk-or-v1-...
CAPTCHA_SOLVER_MODEL=google/gemma-4-31b-it
CAPTCHA_SOLVER_MODEL_FALLBACK=nvidia/nemotron-nano-12b-v2-vl
EOF
chmod 600 ~/.hermes/.env

# 3. Install (builds container, registers MCP, symlinks SKILL)
~/.hermes/plugins/captcha-solver-mcp/install.sh

# 4. Restart Hermes gateway
systemctl --user restart hermes-gateway.service
```

The install script is idempotent — safe to re-run if Docker changes or
OpenRouter model availability shifts.

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

All solvers share a Camoufox browser session launched from `src/browser.py`
with stealth-appropriate defaults: `main_world_eval=True` (so we can reach
page-scripts globals like `window.grecaptcha`), `block_webgl=False`
(reCAPTCHA's fingerprinting probe stalls without WebGL), `os=linux` (Mac/Win
fingerprints crash Firefox in the Linux container).

All VLM calls go through `src/vlm_client.py` — OpenAI-compatible endpoint,
configurable primary + fallback models via env.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required)* | Auth for the VLM endpoint |
| `CAPTCHA_SOLVER_MODEL` | `google/gemma-4-31b-it` | Primary VLM (free tier) |
| `CAPTCHA_SOLVER_MODEL_FALLBACK` | `nvidia/nemotron-nano-12b-v2-vl` | Fallback on 429/5xx |

### Paid-model upgrade for 100% reliability

The free Gemma-4-31B handles easy challenges reliably but struggles with
reCAPTCHA v2's dynamic 4×4 hard mode (which Google serves to flagged
datacenter IPs). For near-100% v2 success, swap to a stronger VLM:

```
CAPTCHA_SOLVER_MODEL=qwen/qwen3-vl-235b-a22b-instruct   # ~$0.01/solve
```

Expected cost on personal-scale CAPTCHA load: $1–$5/month.

## Verification status

| Type | Test target | Result |
|---|---|---|
| reCAPTCHA v3 | `2captcha.com/demo/recaptcha-v3` | **5/5 trials pass**, 8–10s each |
| hCaptcha | `democaptcha.com/demo-form-eng/hcaptcha.html` | Works; multi-page challenges handled |
| Turnstile (fast-path) | `demo.turnstile.workers.dev` | 5s, valid token |
| cf_managed | `nowsecure.nl` | 3.7s, `cf_clearance` cookie |
| reCAPTCHA v2 | `2captcha.com/demo/recaptcha-v2` | Works on 3×3; 4×4 hard-mode is model-bound (~20% with Gemma-4 free, ~95% with Qwen3-VL-235B paid) |

## Integration with Hermes agent

After install + gateway restart, the agent has access to
`captcha-solver:solve` as an MCP tool. A SKILL.md auto-loads at session
start with usage guidance:

- How to detect CAPTCHA type from iframe src / script src
- How to extract the `site_key` from page DOM
- How to inject the returned token or cookies into the agent's own browser
- Cost-awareness notes (OpenRouter quota, free-tier limits)

See `skills/captcha-solver/SKILL.md` for the full agent-facing guide.

## Known limitations

1. **IP cohesion**: tokens are fingerprint + egress-IP bound. The solver and
   the agent MUST share the same public IP. Running the solver on a remote
   host breaks strict-fingerprint sites (Cloudflare especially).
2. **User-agent sync (cf_managed)**: agent must set the UA returned by the
   solver before using `cf_clearance`. Otherwise CF revokes the cookie on
   first use. SKILL.md documents this explicitly.
3. **Model-bound accuracy**: free tier Gemma-4-31B gives ~90% per-tile
   accuracy, which compounds unfavorably on multi-round 4×4 challenges.
   Upgrade to Qwen3-VL-235B (~$0.01/solve) for consistent ~95%+.
4. **No residential proxy built-in**: Camoufox supports `proxy=` at launch;
   wire it in `src/browser.py` if your use case triggers heavy bot detection.

## Development

```sh
# Iterate on solver code
cd ~/.hermes/plugins/captcha-solver-mcp
# edit src/solvers/*.py
docker compose up -d --build
# hit the API directly
curl -X POST http://127.0.0.1:8899/solve \
  -H 'Content-Type: application/json' \
  -d '{"type":"recaptcha_v3","site_url":"...","site_key":"...","timeout_s":60}'
```

Run the smoke-test suite: `./test.sh` (needs container up + OpenRouter key).

## Licensing

MIT (see `src/LICENSES/NOTICE` for third-party attribution). Camoufox (MPL
2.0), patchright (Apache 2.0), and other deps are compatible. No GPL-3.0
contamination — hCaptcha solving is in-house, not via `hcaptcha-challenger`.

## Authors

- [atebites-hub](https://github.com/atebites-hub) — design, integration
- Claude Opus 4.7 — implementation assistance

## Credits

- [aydinnyunus/ai-captcha-bypass](https://github.com/aydinnyunus/ai-captcha-bypass) — reCAPTCHA v2 flow reference
- [daijro/camoufox](https://github.com/daijro/camoufox) — stealth Firefox
- [OpenRouter](https://openrouter.ai/) — VLM endpoint + free-tier Gemma-4
