---
name: captcha-solver
description: When and how to call the captcha-solver MCP to get past reCAPTCHA v2/v3, hCaptcha (incl. Enterprise), Turnstile, and Cloudflare Managed Challenge. Covers type detection, site-key extraction, token/cookie injection, cost awareness.
version: 0.1.0
author: spawdog
license: GPL-3.0
metadata:
  hermes:
    tags: [MCP, CAPTCHA, browser, OpenRouter, Cloudflare, hCaptcha, reCAPTCHA]
    related_skills: [zai-mcp, browser-automation]
---

# captcha-solver MCP

You have a dedicated CAPTCHA solver available as `captcha-solver:solve`. It
runs its own stealth Firefox (Camoufox) in a Docker container and calls a
strong vision model (OpenRouter Qwen3-VL/Gemma/etc.) to classify tiles. It
handles the full challenge flow end-to-end and returns either a response
token or a `cf_clearance` cookie.

## When to call it

As soon as you detect a CAPTCHA blocking your progress on a page. Don't try to
screenshot-then-zai-vision yourself — that's minutes per solve; this tool is
~15-60s.

## How to detect the CAPTCHA type

Inspect the page for one of these signatures:

| Signature | `type` value |
|---|---|
| `iframe[src*='www.google.com/recaptcha/api2']` or "I'm not a robot" checkbox | `recaptcha_v2` |
| `<script src='https://www.google.com/recaptcha/api.js?render=<key>'>` (no visible widget) | `recaptcha_v3` |
| `iframe[src*='hcaptcha.com']` or `<div class=h-captcha>` | `hcaptcha` |
| `iframe[src*='challenges.cloudflare.com/turnstile']` or `<div class=cf-turnstile>` | `turnstile` |
| Full-page CF interstitial with `#challenge-stage` / "Checking your browser before accessing" / "Just a moment…" | `cf_managed` |

## How to extract the site_key

Required for every type except `cf_managed`. Sources (in order of preference):

1. The widget's `data-sitekey` attribute — `div[data-sitekey]` or
   `div.g-recaptcha[data-sitekey]` / `div.h-captcha[data-sitekey]` /
   `div.cf-turnstile[data-sitekey]`.
2. URL params on the loader script — e.g. `render=<key>` on reCAPTCHA v3's
   `api.js` URL.
3. Regex match on the page source — reCAPTCHA and Turnstile keys look like
   `6L...` and hCaptcha / Turnstile keys can look like UUIDs.

## How to call the tool

```
captcha-solver:solve(
    type="recaptcha_v2",       # required; one of the 5 above
    site_url="https://...",    # the full URL of the page hosting the widget
    site_key="6L...",          # required except for cf_managed
    timeout_s=180              # optional, default 180, max 600
)
```

Returns JSON. Two possible shapes:

- **Token response** (reCAPTCHA/hCaptcha/Turnstile):
  ```json
  {"token": "...", "model_used": null, "elapsed_ms": 57500}
  ```
- **Cookie bundle** (cf_managed only):
  ```json
  {"cookies": {"cf_clearance": "...", "__cf_bm": "..."},
   "user_agent": "Mozilla/5.0 ...",
   "elapsed_ms": 12500}
  ```

## How to inject the result

### Token (reCAPTCHA / hCaptcha / Turnstile)

Response field IDs:
- reCAPTCHA v2/v3 → `g-recaptcha-response` (hidden textarea)
- hCaptcha → `h-captcha-response` (hidden textarea)
- Turnstile → `cf-turnstile-response` (hidden input)

Set the value AND dispatch events so any JS form-validation runs:

```js
const id = 'g-recaptcha-response';  // or h-captcha-response, cf-turnstile-response
const el = document.getElementById(id) || document.querySelector(`[name=${id}]`);
el.value = token;
el.dispatchEvent(new Event('input',  {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
```

Then submit the form normally.

### Cookie bundle (cf_managed)

This is stricter. The `cf_clearance` cookie is **bound to the exact User-Agent
that earned it** — you MUST sync your browser's UA before using the cookie.

1. Relaunch your Camoufox context with the returned `user_agent` (pass it as
   `user_agent=` when creating the context, OR set it via
   `context.set_extra_http_headers({"User-Agent": ua})`).
2. Add each cookie to the context: for each `name, value` in `cookies`:
   `context.add_cookies([{"name": name, "value": value, "domain": "<host>", "path": "/"}])`
   (use the host from `site_url`).
3. Navigate to `site_url` — the interstitial should be bypassed for ~30
   minutes (typical `cf_clearance` TTL).

**If the UA doesn't match, CF revokes `cf_clearance` on first use.** Don't
skip step 1.

## Cost awareness

OpenRouter usage is metered:
- Free tier: 50 req/day at $0 credit; 1000/day once you've deposited any money.
- Vision models are token-metered — each CAPTCHA takes ~30-50k input tokens
  (tile images inlined as base64) + 1-2k output tokens.
- At free-tier models (Gemma-4-31b): effectively $0/solve.
- At paid flagships (Qwen3-VL-235B): ~$0.01-0.03/solve.

Don't call the solver in tight retry loops. If the first solve fails, surface
the error to the user and let them decide. Two failures in a row almost always
means the widget isn't on the page (wrong `site_url`) or the site is using a
variant we don't handle (hCaptcha Enterprise with an unsupported challenge
type, for instance).

## Failure modes

- **`501 Not Implemented`** — check if you passed the right `type`. The five
  supported values are listed above.
- **`504 TimeoutError`** — solver exhausted `timeout_s` without producing a
  token. Usually means the widget didn't render on the page, or the selectors
  changed. Ask the user to verify the `site_url` loads the CAPTCHA visibly.
- **`500 RuntimeError: could not find or click the reCAPTCHA checkbox`** —
  no reCAPTCHA widget detected. Re-check the page / site_key.
- **`429 Rate limit`** from OpenRouter — you're out of free-tier budget.
  Surface to the user with the exact 429 body so they can deposit credit.
- **Cookie from cf_managed rejected** — UA sync failed (see above).

## Related tools in your stack

- `zai-vision:*` — image analysis for content the user sends (not CAPTCHAs)
- `zai-reader:webReader` — fetch URL content as clean markdown after CAPTCHA
  is past
- `zai-zread:*` — public GitHub repos
- The browser toolset (agent-browser / Camoufox you drive directly) is still
  what you use to navigate to the CAPTCHA page and inject the token after
  solving.
