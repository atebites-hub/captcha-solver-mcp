# captcha-solver-mcp

Containerized CAPTCHA solver bundled as a Hermes plugin. Handles:

- reCAPTCHA v2 (vision, image-grid challenges)
- reCAPTCHA v3 (token harvest, score-based)
- hCaptcha — public + Enterprise (via upstream `hcaptcha-challenger` +
  custom OpenRouter provider shim)
- Cloudflare Turnstile
- Cloudflare Managed Challenge (returns `cf_clearance` cookie + UA)

Exposes one MCP tool: `captcha-solver:solve(type, site_url, site_key, timeout_s)`.

## Architecture

```
Hermes gateway ─┬─ MCP stdio shim (src/mcp_shim.py)
                │    │  HTTP POST
                │    ▼
                │  FastAPI on 127.0.0.1:8899 (src/server.py)
                │    │
                │    ├─ RecaptchaV2Solver  (Camoufox + OpenRouter VLM)
                │    ├─ RecaptchaV3Harvester
                │    ├─ HcaptchaSolver     (hcaptcha-challenger + OpenAIProvider)
                │    ├─ TurnstileSolver
                │    └─ CloudflareManagedChallenge
                │
                └─ zai-mcp, silverbullet-space, hermes-lcm (unrelated plugins)
```

## Prereqs

1. **OpenRouter API key** — https://openrouter.ai/settings/keys.
   Free tier = 50 req/day; bumps to 1000/day once any credit is deposited.
2. Docker on the host, accessible by `admin`.
3. ~1.5 GB disk (image + Camoufox Firefox binary + GeoIP DB).

Add to `~/.hermes/.env`:

```
OPENROUTER_API_KEY=sk-or-v1-...
CAPTCHA_SOLVER_MODEL=google/gemma-4-31b-it                         # free, OK for reCAPTCHA v2
# CAPTCHA_SOLVER_MODEL=qwen/qwen3-vl-235b-a22b-instruct            # paid, best for hCaptcha Enterprise
CAPTCHA_SOLVER_MODEL_FALLBACK=nvidia/nemotron-nano-12b-v2-vl       # free, on 429 from primary
```

## Install

```sh
ln -s /home/admin/obsidian/vaults/_plugins/captcha-solver-mcp \
      /home/admin/.hermes/plugins/captcha-solver-mcp

/home/admin/.hermes/plugins/captcha-solver-mcp/install.sh

systemctl --user restart hermes-gateway.service
```

Installer verifies the configured model is live on OpenRouter, builds the
container, waits for `/health`, merges the MCP entry into
`~/.hermes/config.yaml`, and symlinks SKILL.md into Hermes's skills tree for
auto-discovery.

## Uninstall

```sh
/home/admin/.hermes/plugins/captcha-solver-mcp/uninstall.sh
systemctl --user restart hermes-gateway.service
```

Removes the container + image, strips the MCP entry from config.yaml, removes
the SKILL symlink. Leaves `_plugins/captcha-solver-mcp/` source in place for
later re-install.

## Testing

```sh
# From the plugin dir, after install.sh has run:
./test.sh
```

Runs smoke tests for each CAPTCHA type against public demo sites and prints
pass/fail. Some tests (hCaptcha Enterprise in particular) can cost a few cents
of OpenRouter usage if you're on a paid model.

Manual single tests:

```sh
curl -X POST http://127.0.0.1:8899/solve \
  -H 'Content-Type: application/json' \
  -d '{"type":"recaptcha_v2","site_url":"https://2captcha.com/demo/recaptcha-v2","site_key":"6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u","timeout_s":180}'
```

## Cost budget

| Type | Calls per solve | Model | Per-solve cost (approx) |
|---|---|---|---|
| reCAPTCHA v2 | 10–40 | Gemma-4-31b (free) | $0 |
| reCAPTCHA v3 | 0 | none | $0 |
| hCaptcha public | 5–15 | Qwen3-VL or Gemma | $0–0.02 |
| hCaptcha Enterprise | 15–40 | Qwen3-VL (recommended) | $0.01–0.05 |
| Turnstile | 0 | none | $0 |
| CF Managed | 0–5 | inherits from sub-challenge | $0–0.02 |

Typical personal-scale monthly spend: **$0–$3**. If you loop on failures,
OpenRouter 429s catch you within an hour.

## Licensing (IMPORTANT)

This container is distributed under **GPL-3.0** because of the runtime
dependency on `hcaptcha-challenger` (GPL-3.0-or-later). Private use on your
own infrastructure is unrestricted. Publishing the container image requires
GPL-3.0 compliance.

See `src/LICENSES/NOTICE` for full third-party attribution.

## Known limitations

- **IP cohesion**: `cf_clearance` cookies bind the solver container's egress
  IP. Solver and agent must share egress (same VPS). Running solver on a
  different host breaks CF.
- **UA sync**: agent MUST use the returned `user_agent` when injecting
  `cf_clearance`, otherwise CF revokes the cookie on first use.
- **Challenger API drift**: `hcaptcha-challenger` is actively developed and
  occasionally changes its internal surface. Pin a working version in
  `requirements.txt` and test after each upgrade.
- **Model-specific accuracy**: reCAPTCHA v2 tile classification is easy for
  most VLMs (Gemma-4-31b works). hCaptcha Enterprise spatial reasoning (drag,
  rotate, click-center) is harder — recommend Qwen3-VL-235B or similar
  flagship. If the model starts refusing CAPTCHA-flavored prompts (as
  Claude/GPT-4o increasingly do), swap to a more permissive model.
- **No per-hour spend cap** in v0.1.0. A runaway agent could drain
  OpenRouter credit. Future work: add a budget guard in `vlm_client.py`.

## Re-enabling web search

`web_search_prime` and the non-prime `web_search` endpoints on Z.AI are both
broken/restricted for this account (tested 2026-04-20). If Z.AI fixes it, the
zai-mcp plugin has the re-enable snippet, not this one.

## References

- [QIN2DIM/hcaptcha-challenger](https://github.com/QIN2DIM/hcaptcha-challenger) — GPL-3.0, the hCaptcha solver we depend on
- [aydinnyunus/ai-captcha-bypass](https://github.com/aydinnyunus/ai-captcha-bypass) — reference for the reCAPTCHA v2 vision flow
- [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver) — reference for Turnstile
- [daijro/camoufox](https://github.com/daijro/camoufox) — stealth Firefox
- [OpenRouter](https://openrouter.ai/) — pluggable vision-LLM endpoint
