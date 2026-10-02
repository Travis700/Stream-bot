"""Claude API helpers: structured JSON answers with optional images."""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import anthropic

from .config import settings

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


class LLMUnavailable(RuntimeError):
    pass


def client() -> anthropic.AsyncAnthropic:
    global _client
    if not settings.llm_enabled:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)
    return _client


def image_block(path: Path) -> dict[str, Any]:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def text_block(text: str, cache: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


async def ask_json(system: str, content: list[dict[str, Any]], schema: dict[str, Any],
                   effort: str | None = None, max_tokens: int = 16000) -> dict[str, Any]:
    """Send one request and return the JSON object that matches ``schema``.

    Uses server-side refusal fallbacks so a false-positive safety decline is retried on
    Anthropic's recommended fallback model instead of failing the job.
    """
    response = await client().beta.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort or settings.claude_effort,
                       "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("Claude returned no text")
    return json.loads(text)
