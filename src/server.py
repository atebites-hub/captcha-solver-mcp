"""FastAPI dispatcher for the captcha-solver container.

Exposes POST /solve with {type, site_url, site_key?} and routes to the right
solver module under src/solvers/. Each solver returns either a token (reCAPTCHA,
hCaptcha, Turnstile) or a cookies+UA bundle (CF Managed Challenge).

Runs on 127.0.0.1:8899 inside the container; the host-side MCP shim
(src/mcp_shim.py) proxies MCP tool calls to here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("captcha_solver")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(title="captcha-solver-mcp", version="0.1.0")


CaptchaType = Literal[
    "recaptcha_v2",
    "recaptcha_v3",
    "hcaptcha",
    "turnstile",
    "cf_managed",
]


class SolveRequest(BaseModel):
    type: CaptchaType = Field(..., description="CAPTCHA variety to solve")
    site_url: str = Field(..., description="Full URL of the page hosting the CAPTCHA")
    site_key: str | None = Field(
        None,
        description=(
            "Public site-key / data-sitekey. Required for reCAPTCHA + hCaptcha + "
            "Turnstile; ignored for cf_managed (detected from the page)."
        ),
    )
    # Optional tuning knobs — not required for v0.1.0, reserved for future use.
    timeout_s: int = Field(180, ge=10, le=600)


class TokenResponse(BaseModel):
    token: str
    model_used: str | None = None
    elapsed_ms: int


class CookieBundleResponse(BaseModel):
    cookies: dict[str, str]
    user_agent: str
    elapsed_ms: int


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": app.version}


@app.post("/solve")
async def solve(req: SolveRequest) -> JSONResponse:
    """Dispatch a solve request to the type-specific solver."""
    t0 = time.monotonic()
    logger.info("solve type=%s url=%s", req.type, req.site_url)

    # Lazy-import solvers so startup doesn't pay for every module and an
    # import error in one solver doesn't sink the whole process.
    try:
        if req.type == "recaptcha_v2":
            from src.solvers.recaptcha_v2 import solve as impl
            token = await impl(req.site_url, req.site_key or "", req.timeout_s)
            return JSONResponse(TokenResponse(
                token=token, elapsed_ms=int((time.monotonic() - t0) * 1000),
            ).model_dump())

        if req.type == "recaptcha_v3":
            from src.solvers.recaptcha_v3 import solve as impl
            token = await impl(req.site_url, req.site_key or "", req.timeout_s)
            return JSONResponse(TokenResponse(
                token=token, elapsed_ms=int((time.monotonic() - t0) * 1000),
            ).model_dump())

        if req.type == "hcaptcha":
            from src.solvers.hcaptcha import solve as impl
            token = await impl(req.site_url, req.site_key or "", req.timeout_s)
            return JSONResponse(TokenResponse(
                token=token, elapsed_ms=int((time.monotonic() - t0) * 1000),
            ).model_dump())

        if req.type == "turnstile":
            from src.solvers.turnstile import solve as impl
            token = await impl(req.site_url, req.site_key or "", req.timeout_s)
            return JSONResponse(TokenResponse(
                token=token, elapsed_ms=int((time.monotonic() - t0) * 1000),
            ).model_dump())

        if req.type == "cf_managed":
            from src.solvers.cf_managed import solve as impl
            cookies, user_agent = await impl(req.site_url, req.timeout_s)
            return JSONResponse(CookieBundleResponse(
                cookies=cookies, user_agent=user_agent,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            ).model_dump())

        # Should be unreachable given Literal validation but be explicit.
        raise HTTPException(status_code=400, detail=f"unknown type: {req.type}")
    except HTTPException:
        raise
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("solve failed for type=%s", req.type)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
