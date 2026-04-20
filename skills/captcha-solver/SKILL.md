---
name: captcha-solver
description: Classifier MCP for reCAPTCHA/hCaptcha tile challenges. You drive your own Camofox browser; this tool takes per-tile screenshots and returns which indices contain the target.
version: 0.2.0
author: spawdog
license: MIT
metadata:
  hermes:
    tags: [MCP, CAPTCHA, browser, Camofox, vision]
    related_skills: [browser-automation]
---

# captcha-solver MCP

A **tile classifier** — not a full browser-driving solver. The agent (you)
owns the browser session and orchestrates the challenge. The MCP just takes
per-tile images and returns which indices match the target.

This arrangement matters because Hermes and the solver both need to share
a browser fingerprint for Google/Cloudflare tokens to validate. Since
Hermes already runs on Camofox, you're the one with the valid session —
only the vision classification is delegated.

## Tool surface

```
captcha-solver:solve_tiles(
    target: str,                # "crosswalks", "buses", "traffic lights", ...
    tile_images: [base64-png],  # order matters; we return indices into this list
    instruction_image: base64-png  # optional; overrides target if supplied
)
```

Returns:
```json
{
  "match_indices": [0, 4, 8],
  "target": "crosswalk",
  "model_used": "glm-5v-turbo",
  "per_tile_latency_ms": [1200, 1100, ...],
  "total_elapsed_ms": 3400
}
```

## How to solve reCAPTCHA v2 3×3 / 4×4

1. **Navigate to the page.** `browser_navigate(url)`. If the page loads a
   "I'm not a robot" checkbox, click it to open the challenge. Call
   `browser_snapshot` again after the modal appears.

2. **Read the instruction.** The snapshot will show something like
   `heading "Select all images with crosswalks"`. Parse the target word(s)
   out of it. Or, if parsing is flaky, take a screenshot of the heading
   element and pass it as `instruction_image` — we'll re-read it.

3. **Collect per-tile screenshots.** The snapshot lists tiles as
   interactive refs — typically buttons or images inside a grid role. For
   each tile ref, call `browser_screenshot_element(ref)` and collect the
   returned `image_base64` strings in order.

4. **Classify.** Call:
   ```
   captcha-solver:solve_tiles(
       target="crosswalk",
       tile_images=[b64_0, b64_1, ..., b64_15]
   )
   ```
   You'll get back `match_indices: [0, 3, 7, ...]`.

5. **Click matches.** For each `i` in `match_indices`, call
   `browser_click(ref=tile_refs[i])`. Keep the local mapping from index
   to ref — we return indices because we don't see your snapshot.

6. **Handle dynamic 4×4 fade-in.** reCAPTCHA's hard mode fades new tiles
   into slots you just clicked. After clicking, wait ~1s, take a fresh
   `browser_snapshot` (tile refs may have been re-numbered), screenshot
   the tiles you just clicked again, and call `solve_tiles` on just
   those. Repeat until no more matches (usually 1-3 rounds max).

7. **Submit.** Click the verify button (usually labelled "Verify" or with
   a checkmark icon). If reCAPTCHA accepts, the modal closes and the
   `g-recaptcha-response` textarea gets a token. No token injection
   needed — your browser's session has it.

## How to solve hCaptcha

Similar pattern:
1. Navigate, click the checkbox, snapshot.
2. Read the instruction (`"Please click each image containing a bus"`).
3. Per-tile screenshots + `solve_tiles`.
4. Click matches, submit.

Spatial-reasoning variants (drag-drop, rotate, point-to-region) are not
supported by the tile classifier. If you see one of those, surface to the
user — they're rare on public-use hCaptcha but common on Enterprise.

## Cloudflare Managed Challenge

No tool call needed — Camofox's Firefox fingerprint typically passes CF's
JS challenge without a click. If a Turnstile widget appears on top,
`browser_click` the checkbox and wait; the token lands in
`cf-turnstile-response` on its own.

If CF shows an interactive hCaptcha, follow the hCaptcha flow above.

## Cost awareness

Classification uses our own Z.AI Coding-plan subscription (via the
`X-Title: 4.5V MCP Local` hack documented in the [[Z.AI Vision Hack]]
buildbook). Per-call cost is effectively zero — it counts against the
5-hour prompt pool, not a per-token wallet. No OpenRouter credits involved.

Still, don't loop: each `solve_tiles` call makes N parallel VLM requests
with thinking enabled (~2-8s each). A stuck agent retrying 10× wastes the
pool.

## Failure modes

- **`{"error": "no browser session"}` on browser_screenshot_element** —
  you forgot to `browser_navigate` first, or the ref was from a stale
  snapshot. Call `browser_snapshot` again.
- **Empty `match_indices`** — classifier saw no target in any tile. If
  you're confident at least one has it, try again with
  `instruction_image` included (the target word may be off).
- **`model_used` is not `glm-5v-turbo`** — check `CAPTCHA_SOLVER_MODEL`
  in `~/.hermes/.env`; should be `glm-5v-turbo` or `glm-4.6v`.
- **CF still blocks after Camofox loads the page** — the IP is flagged.
  Not recoverable from this tool; user needs to rotate Tailscale exit or
  wait out the flag.

## Quick reference: tile-ref mapping trick

Since you're doing `browser_snapshot` → per-tile screenshots → classify →
click, keep the tile refs in the same order you feed them as
`tile_images`. Something like:

```python
tile_refs = ["e12", "e13", "e14", ...]          # from snapshot
tile_imgs = [b64 for _ in (browser_screenshot_element(r) for r in tile_refs)]
result = captcha_solver.solve_tiles(target=t, tile_images=tile_imgs)
for i in result["match_indices"]:
    browser_click(ref=tile_refs[i])
```

Indices line up because you kept the order consistent.
