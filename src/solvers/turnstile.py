"""Cloudflare Turnstile solver — Camoufox-based.

Turnstile renders ONE of several UIs depending on site configuration:
  1. Managed mode, low risk      → widget auto-resolves, no iframe needed
  2. Managed mode, medium risk   → renders iframe with invisible verification
  3. Managed mode, high risk     → renders iframe with visible checkbox
  4. Non-interactive             → no widget, token materializes silently
  5. Invisible                   → no widget, token requires explicit execute()

So we don't chase the iframe — we poll the `cf-turnstile-response` response
field directly, and only try clicking a checkbox if one appears.
"""

from __future__ import annotations

import asyncio
import logging

from src.browser import browser_session

logger = logging.getLogger("captcha_solver.turnstile")

POLL_INTERVAL_S = 0.5
CHECKBOX_CLICK_AFTER_S = 3.0  # try clicking only after the widget has had a chance to render


async def solve(site_url: str, site_key: str, timeout_s: int) -> str:
    """Solve Turnstile on the given page, return the cf-turnstile-response token.

    ``site_key`` is accepted for parity; we read whatever widget is on the page.
    """
    async with browser_session() as (browser, page, _ua):
        # wait_until='load' → defer until script tags finish; Turnstile needs
        # its script to execute before it can populate the response field.
        await page.goto(site_url, wait_until="load", timeout=timeout_s * 1000)

        deadline = asyncio.get_event_loop().time() + timeout_s
        t0 = asyncio.get_event_loop().time()
        checkbox_click_attempted = False

        while asyncio.get_event_loop().time() < deadline:
            token = await _read_token(page)
            if token:
                logger.info("got Turnstile token (len=%d)", len(token))
                return token

            # If a checkbox iframe appears and we haven't clicked yet, try.
            # Don't attempt in the first few seconds — let invisible/managed
            # variants resolve silently first.
            elapsed = asyncio.get_event_loop().time() - t0
            if elapsed >= CHECKBOX_CLICK_AFTER_S and not checkbox_click_attempted:
                iframe_count = await page.locator(
                    "iframe[src*='challenges.cloudflare.com']"
                ).count()
                if iframe_count > 0:
                    logger.info("Turnstile iframe present — attempting checkbox click")
                    try:
                        widget = page.frame_locator(
                            "iframe[src*='challenges.cloudflare.com']"
                        )
                        cb = widget.get_by_role("checkbox")
                        await cb.click(timeout=5000)
                    except Exception as exc:
                        # Non-fatal — invisible Turnstile has no checkbox
                        logger.info("checkbox click failed (%s); continuing to poll", exc)
                    checkbox_click_attempted = True

            await asyncio.sleep(POLL_INTERVAL_S)

        raise TimeoutError(
            f"Turnstile did not produce a token within {timeout_s}s"
        )


async def _read_token(page) -> str:
    """Read the hidden cf-turnstile-response input. '' if not yet populated.

    Covers the common attribute patterns — some sites use a hidden <input>,
    others an <textarea>, Turnstile's own injected field lives off [name].
    """
    try:
        return await page.evaluate(
            """
            () => {
                const candidates = [
                    '[name=cf-turnstile-response]',
                    '#cf-turnstile-response',
                    'textarea[name=cf-turnstile-response]',
                    'input[name=cf-turnstile-response]',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el && el.value) return el.value;
                }
                return '';
            }
            """
        )
    except Exception:
        return ""
