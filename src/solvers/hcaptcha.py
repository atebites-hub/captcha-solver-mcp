"""hCaptcha solver (custom, VLM-driven, no library).

We bypass ``hcaptcha-challenger`` — its canvas-vs-task-image expectations and
internal response-queue state machine don't mesh with our Camoufox setup. A
straightforward screenshot + VLM + coordinate-click approach is simpler,
handles every challenge type hCaptcha ships (image-grid, click-on-X, drag,
rotate), and doesn't depend on a particular iframe-class contract.

Flow:
  1. Navigate to site_url
  2. Click the checkbox inside the ``frame=checkbox`` iframe
  3. Wait for the ``frame=challenge`` iframe's ``.challenge-view`` to be visible
  4. Screenshot the challenge iframe
  5. Ask the VLM: given this image, return JSON describing what to click
     ({task: "click_points", points: [{x,y}, ...]} or {task: "unsupported"})
  6. Translate points from iframe coords to page coords (via iframe bounding
     box) and dispatch real mouse clicks
  7. Click submit (``.button-submit``)
  8. Loop until h-captcha-response is populated or we hit max attempts

VLM-driven means accuracy is bounded by the model. Gemma-4-31b gets the easy
grids; Qwen3-VL-235B handles the spatial-reasoning ones. Configurable via
``CAPTCHA_SOLVER_MODEL``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from src.browser import browser_session
from src.vlm_client import VLMClient

logger = logging.getLogger("captcha_solver.hcaptcha")

MAX_CHALLENGE_ATTEMPTS = 8
SUBMIT_RETRY_INTERVAL_S = 0.5


async def solve(site_url: str, site_key: str, timeout_s: int) -> str:
    vlm = VLMClient()

    async with browser_session(humanize=True) as (browser, page, _ua):
        await page.goto(site_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        await page.wait_for_timeout(2000)

        # Fast-path: some sites auto-pass for clean fingerprints
        token = await _read_token(page)
        if token:
            logger.info("hCaptcha auto-passed (no challenge)")
            return token

        # ── 1. Click the checkbox ─────────────────────────────────────────
        try:
            await page.frame_locator("iframe[src*='frame=checkbox']").locator(
                "#checkbox"
            ).click(timeout=30_000)
            logger.info("hCaptcha checkbox clicked")
        except Exception as exc:
            raise RuntimeError(
                f"could not click hCaptcha checkbox on {site_url}: {exc}"
            )
        await page.wait_for_timeout(1500)

        # Another fast-path after the click (hCaptcha may grant without challenge)
        token = await _read_token(page)
        if token:
            logger.info("hCaptcha passed on checkbox click alone")
            return token

        # Ensure viewport is tall enough to contain the whole challenge iframe
        # (hCaptcha's iframe is often taller than a default 720px viewport).
        await page.set_viewport_size({"width": 1280, "height": 1600})

        # ── 2. Challenge loop ─────────────────────────────────────────────
        challenge_iframe_selector = "iframe[src*='frame=challenge']"
        for attempt in range(MAX_CHALLENGE_ATTEMPTS):
            logger.info("challenge attempt %d/%d", attempt + 1, MAX_CHALLENGE_ATTEMPTS)
            challenge = page.frame_locator(challenge_iframe_selector)

            # Wait for .challenge-view to actually appear AND stabilize
            try:
                await challenge.locator(".challenge-view").first.wait_for(timeout=15_000)
            except Exception:
                logger.info("no .challenge-view visible — maybe already passed")
                break

            # Give images time to load and layout to stabilize. hCaptcha often
            # renders the frame empty first then paints images over 1-2s.
            await page.wait_for_timeout(2000)

            # Scroll the iframe into view so mouse coords line up
            iframe_handle = page.locator(challenge_iframe_selector).first
            await iframe_handle.scroll_into_view_if_needed(timeout=5000)
            await page.wait_for_timeout(500)

            bbox = await iframe_handle.bounding_box()
            if not bbox:
                raise RuntimeError("challenge iframe has no bounding box")
            iframe_png = await iframe_handle.screenshot()

            # ── 3. Ask VLM what to do ────────────────────────────────────
            action = await _ask_vlm(vlm, iframe_png)
            logger.info("VLM action: %s", action)

            task = action.get("task") or "unsupported"
            if task == "already_solved":
                logger.info("VLM reports challenge already complete")
                break
            if task == "unsupported":
                # Try refreshing the challenge — hCaptcha randomly serves
                # different challenge types; a refresh often lands a simpler one.
                reason = action.get("reason", "unknown")
                logger.info("unsupported challenge (%s) — trying refresh", reason)
                try:
                    await challenge.locator(".refresh, .refresh-on, [aria-label*='refresh']").first.click(timeout=3000)
                    await page.wait_for_timeout(2000)
                    continue
                except Exception:
                    raise RuntimeError(
                        f"hCaptcha challenge unsupported and refresh failed: {reason}"
                    )

            # ── 4. Execute the action ────────────────────────────────────
            if task == "click_points":
                points = action.get("points") or []
                if not points:
                    raise RuntimeError("VLM returned click_points task but empty points list")
                for pt in points:
                    try:
                        x = float(pt["x"])
                        y = float(pt["y"])
                    except (KeyError, ValueError, TypeError):
                        logger.warning("skipping malformed point %r", pt)
                        continue
                    # Translate iframe-relative → page absolute
                    abs_x = bbox["x"] + x
                    abs_y = bbox["y"] + y
                    await page.mouse.move(abs_x, abs_y)
                    await page.wait_for_timeout(80)
                    await page.mouse.click(abs_x, abs_y, delay=40)
                    await page.wait_for_timeout(200)
                logger.info("clicked %d points", len(points))
            elif task == "drag":
                drags = action.get("drags") or []
                if not drags:
                    raise RuntimeError("VLM returned drag task but empty drags list")
                for d in drags:
                    try:
                        sx, sy = float(d["from"]["x"]), float(d["from"]["y"])
                        ex, ey = float(d["to"]["x"]), float(d["to"]["y"])
                    except (KeyError, ValueError, TypeError):
                        logger.warning("skipping malformed drag %r", d)
                        continue
                    # Translate to page-absolute
                    abs_sx, abs_sy = bbox["x"] + sx, bbox["y"] + sy
                    abs_ex, abs_ey = bbox["x"] + ex, bbox["y"] + ey
                    await page.mouse.move(abs_sx, abs_sy)
                    await page.wait_for_timeout(100)
                    await page.mouse.down()
                    await page.wait_for_timeout(100)
                    # Smooth drag with interpolation steps for realism
                    await page.mouse.move(abs_ex, abs_ey, steps=20)
                    await page.wait_for_timeout(100)
                    await page.mouse.up()
                    await page.wait_for_timeout(300)
                logger.info("performed %d drag(s)", len(drags))
            elif task == "click_tiles":
                tiles = action.get("tiles") or []
                for idx in tiles:
                    tile = challenge.locator(f".task-image").nth(int(idx))
                    try:
                        await tile.click(timeout=3000)
                    except Exception as exc:
                        logger.warning("tile %d click failed: %s", idx, exc)
            else:
                raise RuntimeError(f"VLM returned unknown task: {task}")

            # ── 5. Submit ────────────────────────────────────────────────
            # Submit button lives inside the iframe's internal scroll region
            # and may be outside the main-page viewport. Use dispatchEvent
            # (bypasses Playwright's viewport checks) — the button handler
            # doesn't care about viewport, just the click.
            try:
                submit_btn = challenge.locator(".button-submit")
                await submit_btn.evaluate("el => el.click()")
                logger.info("submitted via .click()")
            except Exception as exc:
                logger.warning("submit via .click() failed (%s); trying dispatch", exc)
                try:
                    await submit_btn.dispatch_event("click")
                except Exception as exc2:
                    logger.warning("dispatch_event failed: %s", exc2)
            await page.wait_for_timeout(2500)

            # ── 6. Did we get the token? ──────────────────────────────────
            token = await _read_token(page)
            if token:
                logger.info("hCaptcha solved after %d attempt(s), token len=%d",
                            attempt + 1, len(token))
                return token

            # Otherwise hCaptcha served a new challenge; loop.

        # Final token poll — after the challenge iframe closes, hCaptcha's
        # JS needs a moment to post the token back to the host page's hidden
        # h-captcha-response textarea. Poll for up to 15s.
        deadline = asyncio.get_event_loop().time() + 15
        while asyncio.get_event_loop().time() < deadline:
            token = await _read_token(page)
            if token:
                logger.info("hCaptcha token populated post-solve (len=%d)", len(token))
                return token
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"hCaptcha not solved after {MAX_CHALLENGE_ATTEMPTS} attempts on {site_url}"
        )


async def _ask_vlm(vlm: VLMClient, iframe_png: bytes) -> dict:
    """Single VLM call that decides what action to take for any hCaptcha challenge."""
    prompt = (
        "You are solving an hCaptcha challenge. Analyze the screenshot and "
        "output a single JSON object describing the correct action.\n\n"
        "The screenshot shows the ENTIRE hCaptcha challenge iframe: prompt text "
        "at top, challenge area (images, canvas, or drag targets) in the middle, "
        "submit button at the bottom.\n\n"
        "Output ONE of these JSON shapes:\n\n"
        '  {"task": "click_points", "points": [{"x": <int>, "y": <int>}, ...]}\n'
        "    Use when the challenge asks you to click specific locations. Coords "
        "are pixels relative to the TOP-LEFT of this screenshot. Include ALL "
        "points that satisfy the prompt (e.g. all matching images for 'click the "
        "pairs that are identical'). Minimum 1, typical 2-4 points.\n\n"
        '  {"task": "click_tiles", "tiles": [<index>, ...]}\n'
        "    Use when it's a classic 3x3 or 4x4 image grid where you select "
        "whole tiles. Indices are 0-based, left-to-right top-to-bottom.\n\n"
        '  {"task": "drag", "drags": [{"from": {"x": <int>, "y": <int>}, "to": {"x": <int>, "y": <int>}}, ...]}\n'
        "    Use for drag-to-match, drag-to-position, or 'drag the X to the Y' "
        "challenges. Each drag has a source point (what to grab) and a destination "
        "point (where to drop). Coords are pixels relative to the TOP-LEFT of "
        "this screenshot.\n\n"
        '  {"task": "already_solved"}\n'
        "    Use if the challenge looks already passed (e.g. green checkmark).\n\n"
        '  {"task": "unsupported", "reason": "<short string>"}\n'
        "    Use only for rotation puzzles or anything you genuinely cannot reason about.\n\n"
        "Study the prompt carefully. If the prompt says 'click the pairs that are "
        "IDENTICAL', find all images that are visually the same and return their "
        "center pixels. If it says 'click all X', find every X-containing image "
        "and return its center pixel. For drag challenges, identify each item to "
        "move and its target slot. Only output JSON, no markdown, no prose."
    )
    try:
        response = await vlm.raw(prompt, images=[iframe_png], max_tokens=1024, temperature=0.0)
    except Exception as exc:
        return {"task": "unsupported", "reason": f"VLM call failed: {exc}"}

    text = response.text
    # Strip common markdown fences
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("VLM returned non-JSON: %r", response.text[:500])
        return {"task": "unsupported", "reason": "VLM returned non-JSON"}


async def _read_token(page) -> str:
    """Read h-captcha-response textarea/input. Empty string if not yet populated."""
    try:
        return await page.evaluate(
            """
            () => {
                const el = document.querySelector('[name=h-captcha-response]')
                       || document.querySelector('#h-captcha-response')
                       || document.querySelector('textarea[name=h-captcha-response]');
                return el ? (el.value || '') : '';
            }
            """
        )
    except Exception:
        return ""
