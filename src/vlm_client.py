"""OpenAI-compatible vision client backed by OpenRouter.

Every solver that needs VLM classification/reasoning goes through here. Primary
model is ``CAPTCHA_SOLVER_MODEL`` from env; on rate-limit (429) or 5xx we retry
once against ``CAPTCHA_SOLVER_MODEL_FALLBACK``.

Images are passed in as raw bytes and inlined as base64 data URLs. The OpenAI
Python SDK auto-reads ``OPENAI_BASE_URL`` + ``OPENAI_API_KEY`` at client
construction, but we pass them explicitly so env-var changes between calls are
picked up on the next instantiation (useful when rotating OpenRouter keys).
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


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
ZAI_CODING_VISION_BASE_URL = "https://api.z.ai/api/paas/v4"
# The Z.AI Vision Understanding feature (part of the Coding Plan's expanded
# capabilities, counts against the 5-hour prompt pool — not billed separately)
# unlocks on /paas/v4/chat/completions when the request presents this header.
# Discovered by reverse-engineering @z_ai/mcp-server's chat-service.js.
# Without this header the same request returns 1113 "Insufficient balance."
ZAI_MCP_X_TITLE = "4.5V MCP Local"


def _is_zai_model(model: str) -> bool:
    """Z.AI model ids start with 'glm-' (e.g. glm-4.6v, glm-4.5v, glm-5v-turbo)."""
    return model.lower().startswith("glm-")


@dataclass
class VLMResult:
    text: str
    model_used: str
    prompt_tokens: int
    completion_tokens: int


class VLMClient:
    """Thin wrapper around AsyncOpenAI with primary/fallback and retry."""

    def __init__(
        self,
        primary: str | None = None,
        fallback: str | None = None,
        api_key: str | None = None,
    ):
        self.primary = primary or os.environ.get(
            "CAPTCHA_SOLVER_MODEL", "google/gemma-4-31b-it"
        )
        self.fallback = fallback or os.environ.get(
            "CAPTCHA_SOLVER_MODEL_FALLBACK", "nvidia/nemotron-nano-12b-v2-vl"
        )
        # We maintain two clients — OpenRouter for generic models, and Z.AI's
        # own /paas/v4 endpoint for GLM vision models (routed through the
        # Coding-plan "Vision Understanding" pool via the X-Title header).
        openrouter_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        zai_key = os.environ.get("GLM_API_KEY")
        if not openrouter_key and not zai_key:
            raise RuntimeError(
                "Neither OPENROUTER_API_KEY nor GLM_API_KEY set. "
                "Add one to ~/.hermes/.env and restart the container."
            )
        self.or_client = (
            AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=openrouter_key)
            if openrouter_key else None
        )
        self.zai_client = (
            AsyncOpenAI(base_url=ZAI_CODING_VISION_BASE_URL, api_key=zai_key)
            if zai_key else None
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
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> VLMResult:
        # Pick the right client + extra headers based on model family.
        # GLM vision models (glm-4.6v etc.) route through Z.AI's /paas/v4 with
        # the MCP-unlock X-Title header, billed against the Coding-plan pool.
        # Everything else routes through OpenRouter.
        if _is_zai_model(model):
            if self.zai_client is None:
                raise RuntimeError(
                    f"model {model!r} requires GLM_API_KEY in env but none is set"
                )
            client = self.zai_client
            extra_headers = {
                "X-Title": ZAI_MCP_X_TITLE,
                "Accept-Language": "en-US,en",
            }
            # GLM defaults to thinking-mode on vision models, which eats the
            # whole output budget on simple yes/no classifications and leaves
            # only half-formed reasoning in the response. Disable by default;
            # solvers that want thinking can call raw() with the appropriate
            # kwargs (see docstrings).
            extra_body: dict[str, Any] = {"thinking": {"type": "disabled"}}
        else:
            if self.or_client is None:
                raise RuntimeError(
                    f"model {model!r} requires OPENROUTER_API_KEY but none is set"
                )
            client = self.or_client
            extra_headers = {
                "HTTP-Referer": "https://github.com/atebites-hub/silverbullet-vault",
                "X-Title": "hermes-captcha-solver",
            }
            extra_body = {}

        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_headers=extra_headers,
            extra_body=extra_body,
        )
        # Defensive: OpenRouter sometimes returns 200 with null choices on
        # content-policy blocks or upstream 5xx; raising here flows into the
        # retry layer instead of crashing with TypeError.
        if not getattr(resp, "choices", None):
            err_detail = getattr(resp, "error", None) or "empty choices"
            raise RuntimeError(f"VLM returned no choices (model={model}): {err_detail}")
        # GLM thinking-mode sometimes puts content in reasoning_content instead
        # of content; try both.
        msg = resp.choices[0].message
        content = (msg.content or getattr(msg, "reasoning_content", "") or "").strip()
        usage = resp.usage
        return VLMResult(
            text=content,
            model_used=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )

    async def _with_retries(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> VLMResult:
        last_exc: Exception | None = None
        for attempt, model in enumerate([self.primary, self.fallback, self.primary]):
            try:
                return await self._call_once(model, messages, max_tokens, temperature)
            except (RateLimitError, APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                last_exc = exc
                logger.warning(
                    "VLM call failed on attempt %d (model=%s, status=%s): %s",
                    attempt + 1, model, status, exc,
                )
                # Only backoff/retry for 429/5xx; 4xx other than rate-limit
                # is a programming error, not worth retrying.
                if not (isinstance(exc, RateLimitError) or (status and status >= 500)):
                    raise
                await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError(
            f"VLM call failed after 3 attempts (last: {last_exc})"
        ) from last_exc

    async def classify_tile(
        self, image_bytes: bytes, target: str, *, instruction: str | None = None
    ) -> bool:
        """Ask: does this tile contain `target`?  Returns True/False.

        `target` comes from the CAPTCHA instruction bar (e.g. "crosswalks",
        "bicycles"). `instruction` is the full text if you want richer context.
        """
        prompt = (
            f'You are classifying a CAPTCHA tile. Does this image contain a '
            f'{target}? '
            f'{instruction or ""} '
            'Reply with ONLY "YES" or "NO". No other text.'
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
        result = await self._with_retries(messages, max_tokens=8, temperature=0.0)
        answer = result.text.strip().upper()
        # Be lenient: some models wrap in quotes or add punctuation.
        return answer.startswith("YES") or answer == "Y"

    async def extract_instruction(self, image_bytes: bytes) -> str:
        """Read the instruction bar of a CAPTCHA challenge.

        Returns the target object name(s) in lowercase, e.g. "crosswalks",
        "bicycles", "fire hydrants". Empty string if unreadable.
        """
        prompt = (
            "This is the top of a reCAPTCHA/hCaptcha challenge. The instruction "
            "text usually reads 'Select all images with X' or 'Click all images "
            "containing X'. Return ONLY the value of X (the target object), in "
            "lowercase, no punctuation. If you cannot read it, return an empty "
            "string. Example output: crosswalks"
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
        result = await self._with_retries(messages, max_tokens=32, temperature=0.0)
        return result.text.strip().lower().strip(".,!?\"'")

    async def raw(
        self,
        user_prompt: str,
        images: Iterable[bytes] = (),
        *,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> VLMResult:
        """Escape hatch for solvers that need custom prompts (hCaptcha Enterprise)."""
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for img in images:
            content.append(self._inline_image(img))
        messages = [{"role": "user", "content": content}]
        return await self._with_retries(messages, max_tokens=max_tokens, temperature=temperature)

    async def json_response(
        self,
        user_prompt: str,
        schema_hint: str,
        images: Iterable[bytes] = (),
        *,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        """Ask for a JSON response. `schema_hint` is a description of the
        expected shape appended to the prompt (most OpenRouter providers
        don't honor `response_format.json_schema` reliably)."""
        prompt = (
            f"{user_prompt}\n\n"
            f"Respond with ONLY a JSON object matching this shape:\n{schema_hint}\n"
            "Do not include markdown fences or any explanatory text."
        )
        result = await self.raw(prompt, images, max_tokens=max_tokens, temperature=0.0)
        text = result.text
        # Strip common wrapping markdown if the model ignores the instruction.
        for fence in ("```json", "```"):
            if fence in text:
                text = text.split(fence, 1)[-1]
        text = text.strip("` \n\t")
        if text.endswith("```"):
            text = text[:-3].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error("VLM JSON parse failed; raw=%r", result.text)
            raise RuntimeError(f"VLM returned non-JSON: {exc}") from exc
