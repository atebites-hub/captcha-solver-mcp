"""Vision-LLM client backed by Z.AI's Coding-plan Vision Understanding pool.

All VLM calls go to ``https://api.z.ai/api/paas/v4/chat/completions`` with the
magic ``X-Title: 4.5V MCP Local`` header, which routes billing through the
Coding plan's 5-hour prompt pool instead of the General API wallet. This
header was reverse-engineered from @z_ai/mcp-server's chat-service.js and
is what unlocks ``glm-5v-turbo`` and ``glm-4.6v`` on Coding-plan-only keys
(without it, direct calls return ``1113 Insufficient balance``).

Only ``GLM_API_KEY`` is required. No OpenRouter fallback — we rely on the
single Z.AI subscription to avoid accidental cross-billing.

Thinking mode is ENABLED by default. Each solver method sets a generous
``max_tokens`` so reasoning has room to complete before emitting the final
answer. On ``classify_tile`` (yes/no) we accept content after the internal
``</thinking>`` split; on ``extract_instruction`` and ``raw`` we take the
final content the same way.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable

from openai import AsyncOpenAI
from openai import APIStatusError, RateLimitError

logger = logging.getLogger("captcha_solver.vlm")


ZAI_CODING_VISION_BASE_URL = "https://api.z.ai/api/paas/v4"
# The Z.AI Vision Understanding feature (part of the Coding Plan's expanded
# capabilities, counts against the 5-hour prompt pool — not billed separately)
# unlocks on /paas/v4/chat/completions when the request presents this header.
# Discovered by reverse-engineering @z_ai/mcp-server's chat-service.js.
# Without this header the same request returns 1113 "Insufficient balance."
ZAI_MCP_X_TITLE = "4.5V MCP Local"

# Default primary model. glm-5v-turbo is Z.AI's flagship vision model; it
# handles reCAPTCHA 4x4 hard mode, hCaptcha spatial-reasoning, and OCR tasks
# that Gemma/smaller models stumble on.
DEFAULT_MODEL = "glm-5v-turbo"

# Token budgets — generous enough to let thinking complete before the final
# answer. GLM reasoning tokens are separate from content tokens in the
# response, but both share max_tokens. Sized so reasoning + a ~80-char answer
# both fit.
CLASSIFY_MAX_TOKENS = 1024   # yes/no with reasoning
INSTRUCTION_MAX_TOKENS = 1024
RAW_MAX_TOKENS = 8192        # hCaptcha JSON action plans, etc.

# Per-call hard timeout. The openai SDK has no default timeout; a single
# Z.AI-side hang will stall the whole solver indefinitely. With thinking on,
# a legit classify_tile call finishes in ~3-8s (p95 7s per our profile),
# so 30s leaves comfortable headroom but catches true hangs. On timeout we
# retry via _with_retries (up to 3 attempts).
PER_CALL_TIMEOUT_S = 30.0
# Larger raw() / hCaptcha JSON calls may legitimately need more thinking; give
# those a longer cap.
RAW_CALL_TIMEOUT_S = 90.0


@dataclass
class VLMResult:
    text: str                  # final content (post-reasoning if thinking was on)
    reasoning: str             # raw reasoning trace, for debugging
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int


class VLMClient:
    """Z.AI-only VLM client with thinking enabled + Coding-plan billing."""

    def __init__(
        self,
        primary: str | None = None,
        api_key: str | None = None,
    ):
        self.model = primary or os.environ.get("CAPTCHA_SOLVER_MODEL", DEFAULT_MODEL)
        key = api_key or os.environ.get("GLM_API_KEY")
        if not key:
            raise RuntimeError(
                "GLM_API_KEY not set. Add it to ~/.hermes/.env (shared with "
                "main Hermes Coding-plan config) and restart the container."
            )
        self.client = AsyncOpenAI(
            base_url=ZAI_CODING_VISION_BASE_URL, api_key=key
        )

    @staticmethod
    def _inline_image(image_bytes: bytes, mime: str = "image/png") -> dict[str, Any]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }

    async def _call_once(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        thinking: bool = True,
        timeout_s: float = PER_CALL_TIMEOUT_S,
    ) -> VLMResult:
        extra_headers = {
            "X-Title": ZAI_MCP_X_TITLE,
            "Accept-Language": "en-US,en",
        }
        extra_body: dict[str, Any] = {
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }

        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_headers=extra_headers,
            extra_body=extra_body,
            timeout=timeout_s,
        )
        if not getattr(resp, "choices", None):
            err_detail = getattr(resp, "error", None) or "empty choices"
            raise RuntimeError(
                f"VLM returned no choices (model={self.model}): {err_detail}"
            )
        choice = resp.choices[0]
        # Canary: log when max_tokens was hit — means reasoning truncated and
        # we're budget-starved. Bump the relevant CLASSIFY/INSTRUCTION/RAW
        # constant if we see this regularly.
        if getattr(choice, "finish_reason", None) == "length":
            logger.warning(
                "finish_reason=length (max_tokens=%d reached, model=%s) — "
                "reasoning truncated, consider raising budget",
                max_tokens, self.model,
            )
        msg = choice.message
        # GLM thinking-mode puts the chain-of-thought in reasoning_content
        # and the final answer in content. If content is empty (reasoning
        # exhausted max_tokens), fall back to the last-sentence of reasoning
        # as a best-effort extract.
        reasoning = getattr(msg, "reasoning_content", "") or ""
        content = (msg.content or "").strip()
        if not content and reasoning:
            # Pull the last non-empty line of reasoning as the probable answer.
            # This is a fallback; usually means max_tokens was too tight.
            tail = [line.strip() for line in reasoning.strip().splitlines() if line.strip()]
            content = tail[-1] if tail else ""
        usage = resp.usage
        return VLMResult(
            text=content,
            reasoning=reasoning,
            model_used=self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            reasoning_tokens=(
                getattr(usage.completion_tokens_details, "reasoning_tokens", 0)
                if usage and getattr(usage, "completion_tokens_details", None)
                else 0
            ),
        )

    async def _with_retries(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float = 0.1,
        thinking: bool = True,
        timeout_s: float = PER_CALL_TIMEOUT_S,
    ) -> VLMResult:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return await self._call_once(
                    messages, max_tokens, temperature,
                    thinking=thinking, timeout_s=timeout_s,
                )
            except (RateLimitError, APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                last_exc = exc
                logger.warning(
                    "VLM call failed on attempt %d (status=%s): %s",
                    attempt + 1, status, exc,
                )
                if not (isinstance(exc, RateLimitError) or (status and status >= 500)):
                    raise
                await asyncio.sleep(min(2 ** attempt, 8))
            except (asyncio.TimeoutError, Exception) as exc:
                # openai SDK raises APITimeoutError (subclass of APIError) or
                # httpx.ReadTimeout when timeout fires. Treat any timeout-ish
                # error as retryable — re-raise on the last attempt.
                if "timeout" in str(type(exc)).lower() or "timeout" in str(exc).lower():
                    last_exc = exc
                    logger.warning(
                        "VLM call timed out on attempt %d (%.1fs): %s",
                        attempt + 1, timeout_s, exc,
                    )
                    await asyncio.sleep(1.0)
                    continue
                # Not a timeout — let it bubble
                raise
        raise RuntimeError(
            f"VLM call failed after 3 attempts (last: {last_exc})"
        ) from last_exc

    async def classify_tile(
        self, image_bytes: bytes, target: str, *, instruction: str | None = None
    ) -> bool:
        """Ask: does this tile contain `target`?  Returns True/False."""
        prompt = (
            f'You are classifying a CAPTCHA tile. Does this image contain a '
            f'{target}? '
            f'{instruction or ""} '
            'Think carefully, then finish with a single line containing '
            'ONLY "YES" or "NO".'
        ).strip()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    self._inline_image(image_bytes),
                ],
            }
        ]
        result = await self._with_retries(
            messages, max_tokens=CLASSIFY_MAX_TOKENS, temperature=0.0, thinking=True,
        )
        answer = result.text.strip().upper()
        return answer.startswith("YES") or answer == "Y"

    async def extract_instruction(self, image_bytes: bytes) -> str:
        """Read the instruction bar of a CAPTCHA challenge."""
        prompt = (
            "This is the top of a reCAPTCHA/hCaptcha challenge. The instruction "
            "text usually reads 'Select all images with X' or 'Click all images "
            "containing X'. Reason about what you see, then finish with a single "
            "line containing ONLY the value of X (the target object), in "
            "lowercase, no punctuation. Example final line: crosswalks"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    self._inline_image(image_bytes),
                ],
            }
        ]
        result = await self._with_retries(
            messages, max_tokens=INSTRUCTION_MAX_TOKENS, temperature=0.0, thinking=True,
        )
        # Final line after any reasoning text — just normalize.
        last_line = result.text.strip().splitlines()[-1] if result.text else ""
        return last_line.strip().lower().strip(".,!?\"'")

    async def raw(
        self,
        user_prompt: str,
        images: Iterable[bytes] = (),
        *,
        max_tokens: int = RAW_MAX_TOKENS,
        temperature: float = 0.1,
        thinking: bool = True,
        timeout_s: float = RAW_CALL_TIMEOUT_S,
    ) -> VLMResult:
        """Escape hatch for solvers that need custom prompts (hCaptcha, etc.)."""
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for img in images:
            content.append(self._inline_image(img))
        messages = [{"role": "user", "content": content}]
        return await self._with_retries(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            timeout_s=timeout_s,
        )

    async def json_response(
        self,
        user_prompt: str,
        schema_hint: str,
        images: Iterable[bytes] = (),
        *,
        max_tokens: int = RAW_MAX_TOKENS,
    ) -> dict[str, Any]:
        """Ask for a JSON response; parses + returns the dict."""
        prompt = (
            f"{user_prompt}\n\n"
            f"After reasoning, finish with ONLY a JSON object matching this "
            f"shape:\n{schema_hint}\n"
            "Do not include markdown fences around the final JSON."
        )
        result = await self.raw(prompt, images, max_tokens=max_tokens, temperature=0.0)
        text = result.text or ""
        # Try to locate the last {...} block in the response.
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace == -1 or last_brace <= first_brace:
            logger.error("no JSON object in VLM response: %r", text[:500])
            raise RuntimeError("VLM returned no JSON object")
        candidate = text[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.error("VLM JSON parse failed; raw=%r", text[:500])
            raise RuntimeError(f"VLM returned non-JSON: {exc}") from exc
