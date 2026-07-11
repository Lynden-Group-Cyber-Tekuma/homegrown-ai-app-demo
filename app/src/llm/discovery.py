"""Provider model discovery: query provider APIs and persist the results."""
import json
import logging
import re

import httpx

from database import AsyncSessionLocal
from models import AppSetting
from src.llm import catalog

logger = logging.getLogger(__name__)

# ── Provider model discovery ──────────────────────────────────────────────────
_OPENAI_CHAT_EXCLUDE = re.compile(
    r"(embed|tts|whisper|dall-e|davinci-002|babbage-002|omni-moderation|text-moderation|realtime|:ft-)",
    re.IGNORECASE,
)
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")


def _is_chat_model_openai(model_id: str) -> bool:
    if _OPENAI_CHAT_EXCLUDE.search(model_id):
        return False
    return any(model_id.startswith(p) for p in _OPENAI_CHAT_PREFIXES)


async def _discover_provider_models(provider: str, key: str) -> list[str]:
    """Query a provider's models API; return prefixed IDs (e.g. 'openai/gpt-4.1')."""
    results: list[str] = []
    if provider == "openai":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                if r.status_code == 200:
                    for m in r.json().get("data", []):
                        mid = m.get("id", "")
                        if _is_chat_model_openai(mid):
                            results.append(f"openai/{mid}")
                    logger.info("OpenAI discovery: %d chat models found", len(results))
                else:
                    logger.warning("OpenAI /models returned %s", r.status_code)
        except Exception as exc:
            logger.warning("OpenAI model discovery failed: %s", exc)

    elif provider == "anthropic":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
                if r.status_code == 200:
                    for m in r.json().get("data", []):
                        mid = m.get("id", "")
                        if mid:
                            results.append(f"anthropic/{mid}")
                    logger.info("Anthropic discovery: %d models found", len(results))
        except Exception as exc:
            logger.warning("Anthropic model discovery failed: %s", exc)
        if not results:
            # Static fallback for known Claude models not already in config
            results = [
                "anthropic/claude-opus-4-5-20250929",
                "anthropic/claude-sonnet-4-5-20250929",
                "anthropic/claude-haiku-3-5-20241022",
                "anthropic/claude-3-5-sonnet-20241022",
                "anthropic/claude-3-5-haiku-20241022",
                "anthropic/claude-3-opus-20240229",
                "anthropic/claude-3-haiku-20240307",
            ]
            logger.info("Using static Anthropic model list (%d models)", len(results))

    return results


async def _run_discovery(provider: str, key: str) -> None:
    """Background task: discover models for a provider, persist, and refresh cache."""
    discovered = await _discover_provider_models(provider, key)
    if not discovered:
        return
    catalog._DISCOVERED_MODELS[provider] = discovered
    try:
        async with AsyncSessionLocal() as db:
            dm_row = await db.get(AppSetting, "discovered_models")
            payload = json.dumps(catalog._DISCOVERED_MODELS)
            if dm_row:
                dm_row.value = payload
            else:
                db.add(AppSetting(key="discovered_models", value=payload))
            await db.commit()
    except Exception as exc:
        logger.warning("Could not persist discovered models: %s", exc)
    await catalog.refresh_model_cache()
