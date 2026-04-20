"""reCAPTCHA v2 vision solver.

Port of aydinnyunus/ai-captcha-bypass's `recaptcha_v2_test` (main.py:264-416)
to Camoufox + async + OpenRouter VLM. The original submits a demo form to
verify; we return the `g-recaptcha-response` token instead so the caller
(Hermes agent) can inject it into whatever real page it's working on.

Flow:
  1. Navigate to `site_url`
  2. Find the main reCAPTCHA iframe (title='reCAPTCHA'), click the checkbox
  3. Either:
     a. No image challenge appears → success, token already populated
     b. Challenge iframe appears → screenshot instruction bar, extract target
        via VLM; screenshot each of 9 tiles, classify in parallel, click
        matches, click Verify. Repeat up to MAX_CHALLENGE_ATTEMPTS.
  4. Read `g-recaptcha-response` from the hidden textarea, return it.

If the page doesn't contain a reCAPTCHA widget or the widget's site_key
doesn't match, we fail fast.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from typing import Any

from PIL import Image

from src.browser import browser_session
from src.vlm_client import VLMClient

logger = logging.getLogger("captcha_solver.recaptcha_v2")

MAX_CHALLENGE_ATTEMPTS = 8
# After clicking matches, re-screenshot + re-classify just the clicked cells
# this many times to handle reCAPTCHA v2's "dynamic 4x4" hard mode where new
# target tiles fade in where you click. (We only re-check the CLICKED tiles
# not the whole grid — untouched tiles don't change.)
MAX_DYNAMIC_ROUNDS = 3
CLICK_JITTER_MIN = 0.2
CLICK_JITTER_MAX = 0.5

# Upscale per-tile screenshots before classification. Gemma-4 and similar
# small VLMs accuracy-benefit from more pixels to reason over; a 128x128
# tile at 3x → 384x384 is meaningfully clearer for fine-grained object
# detection. Cost: linear increase in base64-encoded payload size.
TILE_UPSCALE_FACTOR = 3

# Fade-stability poll: after clicking matches, poll each clicked tile's
# pixel-hash every N ms and consider the tile stable once its hash hasn't
# changed for STABLE_FOR_MS. Fallback to TIMEOUT_MS cap.
FADE_POLL_INTERVAL_MS = 250
FADE_STABLE_FOR_MS = 600
FADE_TIMEOUT_MS = 4000


async def solve(site_url: str, site_key: str, timeout_s: int) -> str:
    """Solve reCAPTCHA v2 on the given page, return the response token.

    ``site_key`` is accepted for parity with the other solvers but not required
    (we read the widget from whatever is on the page). Passing it lets future
    versions validate we're solving the right widget if multiple are embedded.
    """
    vlm = VLMClient()

    async with browser_session() as (browser, page, _ua):
        await page.goto(site_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)

        # If the page renders the widget dynamically, give it a moment.
        await page.wait_for_timeout(2000)

        # ── Step 1: click the "I'm not a robot" checkbox ────────────────────
        anchor = page.frame_locator("iframe[title='reCAPTCHA']")
        try:
            await anchor.locator(".recaptcha-checkbox-border").click(timeout=10_000)
        except Exception as exc:
            raise RuntimeError(
                f"Could not find or click the reCAPTCHA checkbox at {site_url}: {exc}"
            )
        await page.wait_for_timeout(2000)

        # Short-circuit: sometimes Google accepts a clean fingerprint without
        # a challenge. Check for the token now before committing to the loop.
        token = await _read_token(page)
        if token:
            logger.info("reCAPTCHA passed without image challenge")
            return token

        # ── Step 2: solve image challenges in a loop ────────────────────────
        last_object = ""
        clicked: set[int] = set()
        num_last_clicks = 0

        for attempt in range(MAX_CHALLENGE_ATTEMPTS):
            logger.info("image challenge attempt %d/%d", attempt + 1, MAX_CHALLENGE_ATTEMPTS)

            # Wait for the challenge iframe to appear; if it doesn't, maybe the
            # last round was the winning one — check token and break if so.
            try:
                await page.wait_for_selector(
                    "iframe[title*='recaptcha challenge']", timeout=5000
                )
            except Exception:
                logger.info("no challenge iframe visible — checking token")
                break

            bframe = page.frame_locator("iframe[title*='recaptcha challenge']")

            # Extract instruction text (screenshot the blue bar, ask VLM)
            instr_locator = bframe.locator(".rc-imageselect-instructions")
            try:
                await instr_locator.wait_for(timeout=10_000)
                instr_png = await instr_locator.screenshot()
            except Exception as exc:
                logger.warning("instruction screenshot failed: %s; retry loop", exc)
                continue

            target = (await vlm.extract_instruction(instr_png)).strip()
            # Clean up any leading/trailing decorations the model might add.
            for bad in ("the ", "a ", "an "):
                if target.lower().startswith(bad):
                    target = target[len(bad):]
            logger.info("target identified: %r", target or "<empty>")
            if not target:
                logger.warning("empty target returned; aborting")
                break

            # Track whether object changed (new challenge) vs same (continue)
            if target.lower() != last_object.lower():
                logger.info("new object — resetting clicked tiles")
                clicked = set()
                last_object = target
            elif num_last_clicks >= 3:
                logger.info("same object + >=3 last clicks — likely reset")
                clicked = set()

            # Grab all 9 tiles. reCAPTCHA uses <td> in a .rc-imageselect-table.
            tiles_locator = bframe.locator("table.rc-imageselect-table td")
            n_tiles = await tiles_locator.count()
            if n_tiles == 0:
                # Sometimes it renders as single-image select (rc-image-tile-wrapper)
                tiles_locator = bframe.locator(".rc-imageselect-tile")
                n_tiles = await tiles_locator.count()
            if n_tiles == 0:
                logger.warning("no tiles found in challenge iframe; breaking")
                break

            logger.info("found %d tiles", n_tiles)
            tile_shots = await asyncio.gather(*[
                _safe_screenshot(tiles_locator.nth(i)) for i in range(n_tiles)
            ])

            # Upscale each tile — small source images hurt Gemma-4 accuracy.
            tile_shots = [_upscale(png) if png else None for png in tile_shots]

            # Classify in parallel via VLM
            results = await asyncio.gather(*[
                vlm.classify_tile(png, target, instruction=f"Select all images with {target}.")
                if png else _const(False)
                for png in tile_shots
            ])
            matches = {i for i, yes in enumerate(results) if yes}
            new_clicks = sorted(matches - clicked)
            num_last_clicks = len(new_clicks)
            logger.info(
                "matches=%s already_clicked=%s new=%s",
                sorted(matches), sorted(clicked), new_clicks,
            )

            # Click matches
            import random as _r
            for idx in new_clicks:
                try:
                    await tiles_locator.nth(idx).click()
                    await page.wait_for_timeout(int(_r.uniform(CLICK_JITTER_MIN, CLICK_JITTER_MAX) * 1000))
                except Exception as exc:
                    logger.warning("tile %d click failed: %s", idx, exc)
            clicked.update(new_clicks)

            # Dynamic 4x4 mode: Google fades NEW target tiles in where we
            # just clicked. We only need to re-classify the CLICKED cells —
            # untouched tiles don't change. This keeps the dynamic phase
            # at O(clicked) not O(16) per round.
            recent_clicks = list(new_clicks)  # shrinks each round
            if n_tiles >= 16 and recent_clicks:
                for dyn_round in range(MAX_DYNAMIC_ROUNDS):
                    await _wait_fade_stable(tiles_locator, recent_clicks)
                    dyn_shots = await asyncio.gather(*[
                        _safe_screenshot(tiles_locator.nth(i)) for i in recent_clicks
                    ])
                    dyn_shots = [_upscale(png) if png else None for png in dyn_shots]
                    dyn_results = await asyncio.gather(*[
                        vlm.classify_tile(png, target, instruction=f"Select all images with {target}.")
                        if png else _const(False)
                        for png in dyn_shots
                    ])
                    # Map results back to the subset of indices we asked about
                    dyn_matches = {
                        idx for idx, yes in zip(recent_clicks, dyn_results) if yes
                    }
                    dyn_new = sorted(dyn_matches - clicked)
                    if not dyn_new:
                        logger.info("dynamic round %d: no new matches — done", dyn_round + 1)
                        break
                    logger.info("dynamic round %d: clicking %s", dyn_round + 1, dyn_new)
                    recent_clicks = dyn_new  # next round only re-classifies these
                    for idx in dyn_new:
                        try:
                            await tiles_locator.nth(idx).click()
                            await page.wait_for_timeout(int(_r.uniform(CLICK_JITTER_MIN, CLICK_JITTER_MAX) * 1000))
                        except Exception as exc:
                            logger.warning("dyn tile %d click failed: %s", idx, exc)
                    clicked.update(dyn_new)

            # Click Verify. Verify button is inside the bframe and on tall
            # 4x4 grids can scroll below the iframe's internal viewport —
            # Playwright's auto-scroll doesn't always reach inside iframes.
            # Scroll it in first, then JS-dispatch the click (bypasses
            # viewport-stability checks which fail on iframe-nested elements).
            try:
                verify = bframe.locator("#recaptcha-verify-button")
                await verify.wait_for(state="visible", timeout=5000)
                await verify.scroll_into_view_if_needed(timeout=3000)
                await verify.evaluate("el => el.click()")
                await page.wait_for_timeout(1500)
                # "disabled" attr true → passed; otherwise new challenge shown
                disabled = await verify.get_attribute("disabled")
                if disabled is not None:
                    logger.info("verify button disabled — challenge passed")
                    break
                logger.info("verify button still active — next challenge incoming")
            except Exception as exc:
                logger.info("verify button not found or not clickable (%s) — done", exc)
                break
        else:
            # Loop completed without break → max attempts exhausted
            raise RuntimeError(
                f"reCAPTCHA v2 not solved after {MAX_CHALLENGE_ATTEMPTS} challenge attempts"
            )

        # ── Step 3: read the response token ─────────────────────────────────
        token = await _read_token(page)
        if not token:
            raise RuntimeError(
                "reCAPTCHA v2 loop ended but g-recaptcha-response is empty"
            )
        logger.info("got token (len=%d)", len(token))
        return token


async def _read_token(page: Any) -> str:
    """Read the hidden response textarea. Empty string if not yet populated."""
    try:
        return await page.evaluate(
            "() => document.getElementById('g-recaptcha-response')?.value || ''"
        )
    except Exception:
        return ""


async def _safe_screenshot(locator: Any) -> bytes | None:
    try:
        return await locator.screenshot()
    except Exception as exc:
        logger.warning("screenshot failed: %s", exc)
        return None


def _upscale(png_bytes: bytes) -> bytes:
    """Resize a tile image by TILE_UPSCALE_FACTOR using Lanczos resampling.

    Gemma-4-31b (and other small VLMs) struggle on 96-128px tiles — they get
    a lot more signal from a 3x upscaled version. Lanczos preserves edges
    better than bilinear for this kind of natural-image content.
    """
    try:
        img = Image.open(io.BytesIO(png_bytes))
        w, h = img.size
        new_size = (int(w * TILE_UPSCALE_FACTOR), int(h * TILE_UPSCALE_FACTOR))
        upscaled = img.resize(new_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        upscaled.save(out, format="PNG", optimize=False)
        return out.getvalue()
    except Exception as exc:
        logger.warning("upscale failed, using original: %s", exc)
        return png_bytes


async def _wait_fade_stable(tiles_locator: Any, clicked_indices: list[int]) -> None:
    """Wait until the pixel-hash of every clicked tile stops changing for
    FADE_STABLE_FOR_MS (or we hit FADE_TIMEOUT_MS).

    This replaces a fixed sleep with a signal-driven wait: as soon as fade-in
    animations stop, we proceed. Much more reliable than a 1.5s timer that
    either under- or over-waits depending on the challenge.
    """
    if not clicked_indices:
        return
    loop = asyncio.get_event_loop()
    deadline = loop.time() + FADE_TIMEOUT_MS / 1000
    last_hashes: dict[int, str] = {}
    stable_since: dict[int, float] = {}

    while loop.time() < deadline:
        await asyncio.sleep(FADE_POLL_INTERVAL_MS / 1000)
        now = loop.time()
        shots = await asyncio.gather(*[
            _safe_screenshot(tiles_locator.nth(i)) for i in clicked_indices
        ])
        all_stable = True
        for i, png in zip(clicked_indices, shots):
            if not png:
                continue
            h = hashlib.md5(png).hexdigest()
            if last_hashes.get(i) == h:
                if i not in stable_since:
                    stable_since[i] = now
                elif now - stable_since[i] < FADE_STABLE_FOR_MS / 1000:
                    all_stable = False
            else:
                last_hashes[i] = h
                stable_since.pop(i, None)
                all_stable = False
        if all_stable:
            logger.debug("fade stable for all %d tile(s)", len(clicked_indices))
            return
    logger.debug("fade-stable timeout after %dms", FADE_TIMEOUT_MS)


async def _const(v: Any) -> Any:
    return v
