"""OpenAI-compatible `ChatProvider` for hcaptcha-challenger.

Implements the ``ChatProvider`` Protocol from
`hcaptcha_challenger.tools.internal.providers.protocol` so we can drop it into
each tool's ``_provider`` attribute in place of the default ``GeminiProvider``.

The library's Reasoner base class already supports a ``provider=`` kwarg, but
``RoboticArm.__init__`` doesn't thread one through — it hardcodes Gemini. So
instead of patching the library, we instantiate RoboticArm with a dummy Gemini
key, then walk its tool attributes and replace each ``_provider`` with an
instance of this class.

Challenges with OpenRouter + arbitrary providers:
- Not all providers honor ``response_format.json_schema``. We fall back to
  appending the Pydantic JSON schema directly into the user prompt and
  ``json.loads`` on the text response.
- Some providers return markdown-fenced JSON even when asked not to. We strip
  ```json fences before parsing.
- Multi-image prompts need each image as a separate ``image_url`` content part;
  bytes are base64-inlined.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, TypeVar

from openai import AsyncOpenAI, APIStatusError, RateLimitError
from pydantic import BaseModel, ValidationError

logger = logging.getLogger("captcha_solver.openai_provider")

ResponseT = TypeVar("ResponseT", bound=BaseModel)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenAIProvider:
    """ChatProvider implementation backed by an OpenAI-compatible endpoint.

    Default target is OpenRouter but any OpenAI-compatible URL works by setting
    ``OPENAI_BASE_URL`` in the env. Model is ``CAPTCHA_SOLVER_MODEL`` with
    fallback ``CAPTCHA_SOLVER_MODEL_FALLBACK``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
    ):
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        self.client = AsyncOpenAI(
            base_url=base_url or OPENROUTER_BASE_URL,
            api_key=key,
        )
        self.model = model or os.environ.get(
            "CAPTCHA_SOLVER_MODEL", "google/gemma-4-31b-it"
        )
        self.fallback_model = fallback_model or os.environ.get(
            "CAPTCHA_SOLVER_MODEL_FALLBACK", "nvidia/nemotron-nano-12b-v2-vl"
        )
        self._last_response: Any | None = None

    @staticmethod
    def _inline_image(path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        # Best-effort MIME from the filename; the VLM typically doesn't care.
        mime = "image/png"
        ext = path.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif ext == ".webp":
            mime = "image/webp"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }

    @staticmethod
    def _schema_hint(schema: type[BaseModel]) -> str:
        """Return a compact JSON representation of the model's field shape."""
        try:
            # Pydantic v2: model_json_schema()
            return json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)
        except Exception:
            # Fallback for Pydantic v1 or unusual cases
            return json.dumps(
                {"type": "object", "title": schema.__name__}, indent=2
            )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove common ```json ... ``` fencing the model may add."""
        t = text.strip()
        for fence in ("```json", "```"):
            if t.startswith(fence):
                t = t[len(fence):].strip()
                break
        if t.endswith("```"):
            t = t[:-3].strip()
        return t

    async def _call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> str:
        resp = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_headers={
                "HTTP-Referer": "https://github.com/atebites-hub/silverbullet-vault",
                "X-Title": "hermes-captcha-solver",
            },
        )
        self._last_response = resp
        return (resp.choices[0].message.content or "").strip()

    async def generate_with_images(
        self,
        *,
        images: list[Path],
        response_schema: type[ResponseT],
        user_prompt: str | None = None,
        description: str | None = None,
        **kwargs: Any,
    ) -> ResponseT:
        """Return an instance of ``response_schema`` parsed from the LLM reply.

        hcaptcha-challenger passes path-based images (library writes PNGs to a
        cache dir before invoking the provider). We inline each as base64.
        """
        schema_hint = self._schema_hint(response_schema)
        base_prompt = "\n\n".join(filter(None, [description, user_prompt])) or ""
        full_prompt = (
            f"{base_prompt}\n\n"
            "Respond with ONLY a JSON object matching this JSON Schema:\n"
            f"{schema_hint}\n\n"
            "Do not wrap the JSON in markdown fences or explanatory text. "
            "Output only the JSON object."
        )

        content: list[dict[str, Any]] = [{"type": "text", "text": full_prompt}]
        for p in images:
            content.append(self._inline_image(Path(p)))

        messages = [{"role": "user", "content": content}]

        # Try primary, then fallback. Each gets two parse attempts (the first
        # failure may just be bad JSON formatting — retry to give the model
        # another shot).
        last_exc: Exception | None = None
        for model in [self.model, self.fallback_model]:
            for attempt in range(2):
                try:
                    raw = await self._call(
                        model,
                        messages,
                        max_tokens=kwargs.get("max_tokens", 1024),
                        temperature=kwargs.get("temperature", 0.0),
                    )
                    parsed_text = self._strip_fences(raw)
                    obj = json.loads(parsed_text)
                    return response_schema.model_validate(obj)
                except (RateLimitError, APIStatusError) as exc:
                    status = getattr(exc, "status_code", None)
                    last_exc = exc
                    logger.warning(
                        "openai provider call failed (model=%s, status=%s): %s",
                        model, status, exc,
                    )
                    if isinstance(exc, RateLimitError) or (status and status >= 500):
                        # switch to fallback
                        break
                    raise
                except (json.JSONDecodeError, ValidationError) as exc:
                    last_exc = exc
                    logger.warning(
                        "JSON parse/validate failed on attempt %d (model=%s): %s; "
                        "retrying with stronger prompt",
                        attempt + 1, model, exc,
                    )
                    # Stiffen the prompt and retry once
                    messages[-1]["content"][0]["text"] = (
                        full_prompt
                        + "\n\nIMPORTANT: your previous reply was not valid JSON. "
                        "Return ONLY a single JSON object, no markdown, no prose."
                    )
                    continue
        raise RuntimeError(
            f"OpenAIProvider exhausted retries; last error: {last_exc}"
        ) from last_exc

    def cache_response(self, path: Path) -> None:
        """Best-effort cache of the raw last response for debugging parity
        with GeminiProvider."""
        try:
            if self._last_response is None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "model": self._last_response.model,
                "content": self._last_response.choices[0].message.content,
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            logger.debug("cache_response failed: %s", exc)
