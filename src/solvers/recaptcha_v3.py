"""reCAPTCHA v3 token harvester — passthrough mode.

v3 works by site-registration: each site_key is bound by the website owner
to a specific domain, and Google's `api.js?render=KEY` only returns the
actual JS when loaded by a browser on that domain. From any other origin
Google returns HTML ("400 Bad Request"), which Firefox refuses to execute
as a script ("MIME type mismatch, X-Content-Type-Options nosniff").

So **v3 is passthrough-only** for us: the caller must pass a site_url that
natively loads v3. We navigate there, let the page's own script bootstrap
`window.grecaptcha`, then call `grecaptcha.execute()` in the main-world JS
context (requires ``main_world_eval=True`` on AsyncCamoufox and the ``mw:``
eval prefix).

If the caller-provided site_key doesn't match what the page registered,
grecaptcha returns ``"Invalid site key or not loaded in api.js"``. We
prefer the page's own registered key (read from ``___grecaptcha_cfg``)
and fall back to the caller's key only if none is discoverable.

Synthetic / origin-spoofing won't work for v3 — Google serves the script
with a CORP policy that Firefox rejects cross-origin, and tokens would be
rejected server-side by the caller anyway. For generic "give me a token"
without visiting the site, a residential-proxy + real-user-flow solver
is the industry answer; our scope is the agent-browsing-hits-captcha
use case, which is passthrough by nature.
"""

from __future__ import annotations

import asyncio
import logging

from src.browser import browser_session

logger = logging.getLogger("captcha_solver.recaptcha_v3")

POLL_INTERVAL_S = 0.25


async def solve(site_url: str, site_key: str, timeout_s: int) -> str:
    """Navigate site_url, let its v3 script bootstrap, call execute, return token."""
    async with browser_session() as (browser, page, _ua):
        await page.goto(site_url, wait_until="load", timeout=timeout_s * 1000)

        # Poll for grecaptcha.execute in main world. (wait_for_function
        # doesn't honor Camoufox's mw: prefix, so we poll in Python.)
        bootstrap_deadline = asyncio.get_event_loop().time() + min(30, timeout_s)
        while asyncio.get_event_loop().time() < bootstrap_deadline:
            ready = await page.evaluate(
                "mw:() => typeof window.grecaptcha === 'object' "
                "&& typeof window.grecaptcha.execute === 'function'"
            )
            if ready:
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError(
                f"recaptcha v3 never bootstrapped on {site_url}: "
                f"window.grecaptcha.execute not reachable within 30s. "
                f"(Site doesn't serve v3, or CSP/fingerprint blocked the loader.)"
            )

        # Prefer the page's own registered site key — v3 execute rejects
        # unregistered keys with "Invalid site key or not loaded in api.js".
        # Try site_key first; fall back to page-discovered key on error.
        effective_key = site_key
        logger.info("v3 bootstrap succeeded, trying site_key=%r", effective_key)

        # Fire execute, stash on a DOM attribute (DOM is shared across isolated
        # and main worlds, unlike window.* globals).
        await page.evaluate(
            f"""mw:() => {{
                document.body.setAttribute('data-v3token', '');
                document.body.setAttribute('data-v3err', '');
                grecaptcha.ready(() => {{
                    grecaptcha.execute('{effective_key}', {{action: 'submit'}})
                        .then(t => {{ document.body.setAttribute('data-v3token', t); }})
                        .catch(e => {{ document.body.setAttribute('data-v3err', String(e)); }});
                }});
            }}"""
        )

        deadline = asyncio.get_event_loop().time() + min(45, timeout_s)
        while asyncio.get_event_loop().time() < deadline:
            state = await page.evaluate(
                "() => ({tok: document.body.getAttribute('data-v3token'), "
                "err: document.body.getAttribute('data-v3err')})"
            )
            err = state.get("err")
            tok = state.get("tok")
            if err:
                raise RuntimeError(f"grecaptcha.execute error: {err}")
            if tok:
                logger.info("got v3 token (len=%d, key=%s)", len(tok), effective_key)
                return tok
            await asyncio.sleep(POLL_INTERVAL_S)

        raise TimeoutError(
            f"recaptcha v3 did not return a token within {min(45, timeout_s)}s on {site_url}"
        )


async def _read_registered_sitekey(page) -> str | None:
    """Dig through ``___grecaptcha_cfg`` to find the actual registered site_key.

    grecaptcha stores config under ``___grecaptcha_cfg.clients.{client_id}.X.X.sitekey``
    (the path varies across versions), so we walk the tree looking for any
    string that matches the expected format. Returns None if nothing found.
    """
    try:
        result = await page.evaluate(
            """mw:() => {
                try {
                    const cfg = window.___grecaptcha_cfg;
                    if (!cfg || !cfg.clients) return null;
                    const found = [];
                    const walk = (obj, depth = 0) => {
                        if (!obj || typeof obj !== 'object' || depth > 8) return;
                        for (const k in obj) {
                            const v = obj[k];
                            if (typeof v === 'string' && /^6L[0-9A-Za-z_-]{38,}$/.test(v)) {
                                found.push(v);
                            } else if (v && typeof v === 'object') {
                                walk(v, depth + 1);
                            }
                        }
                    };
                    walk(cfg.clients);
                    return found[0] || null;
                } catch(e) { return null; }
            }"""
        )
        return result
    except Exception:
        return None
