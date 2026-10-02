"""AI helpers: structured JSON answers (with optional images) from Claude or a free local Ollama model.

Callers build content as a list of ``text_block`` / ``image_block`` dicts and call ``ask_json``;
the provider is picked from settings (``LLM_PROVIDER`` / ``RATER_LLM_PROVIDER``).
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
import anthropic

from .config import settings

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


class LLMUnavailable(RuntimeError):
    pass


def client() -> anthropic.AsyncAnthropic:
    global _client
    if not settings.anthropic_api_key:
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


def is_local(purpose: str = "general") -> bool:
    return settings.provider_for(purpose) == "ollama"


async def ask_json(system: str, content: list[dict[str, Any]], schema: dict[str, Any], effort: str | None = None,
                   max_tokens: int = 16000, purpose: str = "general") -> dict[str, Any]:
    """Send one request and return the JSON object that matches ``schema``."""
    provider = settings.provider_for(purpose)
    if provider == "anthropic":
        return await _ask_claude(system, content, schema, effort, max_tokens)
    if provider == "ollama":
        return await _ask_ollama(system, content, schema, max_tokens)
    raise LLMUnavailable(f"AI is turned off (provider {provider!r})")


# ---------------------------------------------------------------- Claude
async def _ask_claude(system: str, content: list[dict[str, Any]], schema: dict[str, Any], effort: str | None,
                      max_tokens: int) -> dict[str, Any]:
    # Server-side refusal fallbacks: a false-positive safety decline is retried on Anthropic's
    # recommended fallback model instead of failing the job.
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


# ---------------------------------------------------------------- Ollama (free, local)
def to_ollama_messages(system: str, content: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Convert our content blocks into an Ollama chat request. Returns (messages, has_images)."""
    texts = [b["text"] for b in content if b["type"] == "text"]
    images = [b["source"]["data"] for b in content if b["type"] == "image"]
    user: dict[str, Any] = {"role": "user", "content": "\n\n".join(texts)}
    if images:
        user["images"] = images
    return [{"role": "system", "content": system}, user], bool(images)


async def _pull_model(session: aiohttp.ClientSession, model: str) -> None:
    log.info("Downloading local AI model %s (first use only, can take several minutes)…", model)
    async with session.post(f"{settings.ollama_url}/api/pull", json={"model": model, "stream": False}) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Could not download model {model}: {body[:300]}")
    log.info("Model %s ready", model)


async def _ask_ollama(system: str, content: list[dict[str, Any]], schema: dict[str, Any],
                      max_tokens: int) -> dict[str, Any]:
    messages, has_images = to_ollama_messages(system, content)
    model = settings.ollama_vision_model if has_images else settings.ollama_text_model
    messages[0]["content"] += "\n\nAnswer only with JSON matching the requested schema."
    payload = {
        "model": model,
        "messages": messages,
        "format": schema,
        "stream": False,
        "keep_alive": "15m",
        "options": {"temperature": 0.2, "num_ctx": settings.ollama_num_ctx, "num_predict": min(max_tokens, 4096)},
    }
    # CPU inference is slow: allow long requests.
    timeout = aiohttp.ClientTimeout(total=60 * 60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        last_error = ""
        pulled = False
        tries = 0
        while tries < 3:
            try:
                async with session.post(f"{settings.ollama_url}/api/chat", json=payload) as resp:
                    body = await resp.text()
            except aiohttp.ClientConnectorError as exc:
                raise LLMUnavailable(f"Can't reach Ollama at {settings.ollama_url}: {exc}") from exc
            if resp.status == 404 and "not found" in body and not pulled:
                await _pull_model(session, model)
                pulled = True
                continue
            tries += 1
            if resp.status != 200:
                raise RuntimeError(f"Ollama error {resp.status}: {body[:300]}")
            text = json.loads(body).get("message", {}).get("content", "")
            try:
                answer = json.loads(text)
            except json.JSONDecodeError:
                last_error = f"invalid JSON from {model}: {text[:200]}"
                log.warning(last_error)
                continue
            missing = [k for k in schema.get("required", []) if k not in answer]
            if missing:
                last_error = f"{model} left out {missing}"
                log.warning(last_error)
                continue
            return answer
    raise RuntimeError(f"Local AI gave no usable answer ({last_error})")
