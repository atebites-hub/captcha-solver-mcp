"""FastAPI tile classifier for the captcha-solver container.

v0.2.0 — classifier-only. The container no longer owns a browser; Hermes
drives its own Camofox session and just hands us tile images. We return
which indices match the target.

Endpoints
---------
POST /solve_tiles
    Input:
        target: str            # e.g. "crosswalks", "buses", "traffic lights"
        tile_images: [str]     # base64-encoded PNG bytes, one per tile
        instruction_image: str (optional)
            A base64 PNG of the instruction bar — if omitted, `target`
            is used verbatim. When present we re-read the instruction
            (reCAPTCHA sometimes rewords it mid-challenge) and override.
        threshold: str = "strict"
            Currently unused; reserved for future tunable behaviour.

    Output:
        {
          "match_indices": [int],   # 0-based indices into tile_images
          "target": str,            # the target we actually classified against
          "model_used": str,
          "per_tile_latency_ms": [int],
          "total_elapsed_ms": int,
        }

GET /health
    {"ok": true, "version": "0.2.0", "model": "<configured VLM>"}
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.vlm_client import VLMClient

logger = logging.getLogger("captcha_solver")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(title="captcha-solver-mcp", version="0.2.0")

_vlm: VLMClient | None = None


def get_vlm() -> VLMClient:
    global _vlm
    if _vlm is None:
        _vlm = VLMClient()
    return _vlm


class SolveTilesRequest(BaseModel):
    target: str = Field(..., description="What to look for in each tile (e.g. 'crosswalks').")
    tile_images: list[str] = Field(
        ..., min_length=1, max_length=64,
        description="Base64-encoded PNG bytes, one per tile. Order matters: the "
                    "returned indices are offsets into this list.",
    )
    instruction_image: str | None = Field(
        None,
        description="Optional base64 PNG of the CAPTCHA instruction bar. When "
                    "supplied, overrides `target` with whatever the VLM reads.",
    )


class SolveTilesResponse(BaseModel):
    match_indices: list[int]
    target: str
    model_used: str
    per_tile_latency_ms: list[int]
    total_elapsed_ms: int


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": app.version,
        "model": os.environ.get("CAPTCHA_SOLVER_MODEL", "glm-5v-turbo"),
    }


def _decode_b64_png(data: str, label: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except Exception as exc:
        raise HTTPException(400, f"{label}: invalid base64 ({exc})") from exc


@app.post("/solve_tiles", response_model=SolveTilesResponse)
async def solve_tiles(req: SolveTilesRequest) -> SolveTilesResponse:
    t0 = time.monotonic()
    vlm = get_vlm()

    target = req.target.strip().lower()
    if req.instruction_image:
        inst_bytes = _decode_b64_png(req.instruction_image, "instruction_image")
        try:
            read_target = await vlm.extract_instruction(inst_bytes)
        except Exception as exc:
            logger.warning("extract_instruction failed, falling back to client target: %s", exc)
            read_target = target
        if read_target and read_target != target:
            logger.info("target overridden by instruction_image: %r -> %r", target, read_target)
            target = read_target

    tiles = [_decode_b64_png(t, f"tile_images[{i}]") for i, t in enumerate(req.tile_images)]

    per_tile_latency: list[int] = [0] * len(tiles)

    async def classify_one(i: int, img: bytes) -> tuple[int, bool]:
        t_start = time.monotonic()
        try:
            hit = await vlm.classify_tile(img, target)
        except Exception as exc:
            logger.warning("tile %d classify failed: %s", i, exc)
            hit = False
        per_tile_latency[i] = int((time.monotonic() - t_start) * 1000)
        return i, hit

    results = await asyncio.gather(*[classify_one(i, img) for i, img in enumerate(tiles)])
    match_indices = sorted(i for i, hit in results if hit)

    total_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "solve_tiles target=%r tiles=%d matches=%d elapsed_ms=%d",
        target, len(tiles), len(match_indices), total_ms,
    )
    return SolveTilesResponse(
        match_indices=match_indices,
        target=target,
        model_used=vlm.model,
        per_tile_latency_ms=per_tile_latency,
        total_elapsed_ms=total_ms,
    )
