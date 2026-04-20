"""Cloudflare Managed Challenge solver.

The Managed Challenge page shows one of several flows depending on CF's threat
assessment:

  1. **JS-only interstitial** — "Checking your browser before accessing…"
     resolves automatically after JS challenge execution. No user interaction.
  2. **Turnstile-wrapped** — interstitial contains a Turnstile checkbox; click
     it to proceed.
  3. **hCaptcha-wrapped** (rare) — interstitial shows an hCaptcha challenge.

The "token" here isn't a reCAPTCHA-style response; it's the ``cf_clearance``
cookie that CF sets once the challenge is satisfied. Subsequent same-origin
requests that present that cookie (+ the SAME user-agent that earned it)
bypass the challenge.

Returns: (cookies_dict, user_agent_string)
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from src.browser import browser_session

logger = logging.getLogger("captcha_solver.cf_managed")

POLL_INTERVAL_S = 0.5
CLEARANCE_COOKIE = "cf_clearance"


async def solve(site_url: str, timeout_s: int) -> tuple[dict[str, str], str]:
    """Navigate the CF interstitial and return (cookies, user_agent).

    Tries up to 2 rounds — CF sometimes re-challenges on first resolution.
    """
    parsed = urlparse(site_url)
    host = parsed.netloc

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            async with browser_session(humanize=True) as (browser, page, user_agent):
                await page.goto(site_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)

                # Poll for the clearance cookie or detect a sub-challenge.
                deadline = asyncio.get_event_loop().time() + timeout_s
                while asyncio.get_event_loop().time() < deadline:
                    # Is cf_clearance cookie already set?
                    context = page.context
                    cookies = await context.cookies(site_url)
                    cookie_map = {c["name"]: c["value"] for c in cookies if c.get("name")}
                    if CLEARANCE_COOKIE in cookie_map:
                        logger.info(
                            "cf_clearance obtained on attempt %d (host=%s)",
                            attempt + 1, host,
                        )
                        return cookie_map, user_agent

                    # Is a Turnstile widget embedded in the interstitial?
                    has_turnstile = await page.evaluate(
                        "() => !!document.querySelector(\"iframe[src*='challenges.cloudflare.com']\")"
                    )
                    if has_turnstile:
                        logger.info("Turnstile sub-challenge detected; clicking checkbox")
                        try:
                            widget = page.frame_locator("iframe[src*='challenges.cloudflare.com']")
                            cb = widget.get_by_role("checkbox")
                            await cb.wait_for(timeout=10_000)
                            await cb.click()
                        except Exception as exc:
                            logger.info("Turnstile click failed (%s); continuing to poll", exc)

                    # Is hCaptcha embedded? Rare, but defer to hcaptcha solver
                    # for the heavy lifting. We call its internals inline
                    # against the current page rather than spawning a new
                    # browser — we need to stay in the SAME session for the
                    # clearance cookie to bind correctly.
                    has_hcaptcha = await page.evaluate(
                        "() => !!document.querySelector(\"iframe[src*='hcaptcha.com']\")"
                    )
                    if has_hcaptcha:
                        # Delegate here in the future; for now surface a clear
                        # error since the hCaptcha solver is Phase 6.
                        raise NotImplementedError(
                            "Managed Challenge with hCaptcha sub-challenge not yet "
                            "supported. Phase 6 will wire this up."
                        )

                    await asyncio.sleep(POLL_INTERVAL_S)

                raise TimeoutError(
                    f"cf_clearance cookie not set within {timeout_s}s on attempt {attempt + 1}"
                )

        except TimeoutError as exc:
            last_err = exc
            logger.warning("attempt %d timed out: %s", attempt + 1, exc)
            if attempt == 0:
                # Brief pause, then retry with a fresh browser session.
                await asyncio.sleep(2.0)
                continue
            raise

    # Shouldn't reach here, but mypy/future-proofing:
    raise RuntimeError(f"cf_managed failed after 2 attempts: {last_err}")
