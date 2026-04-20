"""Camoufox browser fixture + small helpers shared by the solvers.

Typical use:

    async with browser_session() as (browser, page, user_agent):
        await page.goto(site_url)
        ...

`user_agent` is captured right after launch so solvers that need to report it
back to the caller (cf_managed) don't have to interrogate `navigator.userAgent`
themselves later.

Camoufox's `AsyncCamoufox` context manager yields a Playwright-compatible
``Browser``; ``browser.new_page()`` creates a page with the stealth
fingerprint already applied.
"""

from __future__ import annotations

import contextlib
import logging
from typing import AsyncIterator

from camoufox.async_api import AsyncCamoufox

logger = logging.getLogger("captcha_solver.browser")

DEFAULT_TIMEOUT_MS = 60_000


@contextlib.asynccontextmanager
async def browser_session(
    *,
    headless: bool = True,
    locale: str = "en-US",
    humanize: bool = False,
) -> AsyncIterator[tuple[object, object, str]]:
    """Yield a fresh Camoufox browser + page + the UA it chose.

    Each call launches a fresh browser with a randomized stealth fingerprint.
    """
    async with AsyncCamoufox(
        headless=headless,
        locale=locale,
        humanize=humanize,
        # Pin to linux — container is Linux, Mac/Windows fingerprints can
        # cause spurious browser crashes (TargetClosedError) with certain sites.
        os="linux",
        # reCAPTCHA + many anti-bot systems probe WebGL for fingerprinting.
        # If blocked, `recaptcha__en.js` stalls without defining `grecaptcha`.
        # Letting Camoufox spoof a consistent GPU (instead of blocking outright)
        # is the right trade for CAPTCHA solving.
        block_webgl=False,  # Camoufox picks a consistent vendor/renderer
        block_webrtc=True,  # privacy; CAPTCHAs don't use WebRTC
        # CRITICAL: reCAPTCHA's grecaptcha + hCaptcha's hcaptcha globals live
        # in the page's "main world" JS context. Without this flag, Camoufox
        # runs page.evaluate in an isolated world where these globals are
        # invisible. Solvers that need to call grecaptcha.execute / hcaptcha
        # methods must prefix their evaluate script with 'mw:' to run in
        # main world. DOM reads (document.getElementById) work from either.
        main_world_eval=True,
    ) as browser:
        page = await browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        try:
            # Camoufox patches navigator.userAgent in JS, so read it from there
            # rather than guessing based on launch args.
            user_agent = await page.evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = "unknown"
        logger.info("Camoufox launched. UA=%s", user_agent)
        try:
            yield browser, page, user_agent
        finally:
            pass  # AsyncCamoufox closes the browser on context exit
