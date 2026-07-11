"""Provider detection and direct OpenAI-compatible client construction."""
import json
import logging

from openai import AsyncOpenAI

from crypto import decrypt
from models import User
from src.core.config import _SHARED_LLM_KEYS

logger = logging.getLogger(__name__)

# ── Provider base URLs for direct calls (OpenAI-compatible endpoints) ────────
_PROVIDER_URLS = {
    "openai":     "https://api.openai.com/v1",
    "anthropic":  "https://api.anthropic.com/v1",
    "google":     "https://generativelanguage.googleapis.com/v1beta/openai/",
    "perplexity": "https://api.perplexity.ai",
    "openrouter": "https://openrouter.ai/api/v1",
}



def _detect_provider(model_id: str) -> str:
    # Handle explicit provider-prefixed IDs (e.g. from model discovery)
    if model_id.startswith("openai/"):
        return "openai"
    if model_id.startswith("anthropic/"):
        return "anthropic"
    if model_id.startswith("perplexity/"):
        return "perplexity"
    if model_id.startswith("google/"):
        return "google"
    m = model_id.lower()
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if m.startswith("claude-"):
        return "anthropic"
    if m.startswith("gemini-"):
        return "google"
    if m.startswith(("sonar", "r1-1776")):
        return "perplexity"
    return "openrouter"


def _model_meta(model_id: str) -> dict:
    """Return category, provider, and required key info for a model."""
    provider = _detect_provider(model_id)
    is_free = model_id.lower().endswith(":free")
    return {
        "category": "free" if is_free else "paid",
        "provider": {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "perplexity": "Perplexity", "openrouter": "OpenRouter"}[provider],
        "requires_key": "openrouter" if is_free else provider,
    }


def _get_llm_key(user: User, model_id: str) -> str:
    """Returns the best available API key for model_id: per-user → shared .env → empty."""
    provider = _detect_provider(model_id)
    if user.llm_api_keys_enc:
        try:
            keys = json.loads(decrypt(user.llm_api_keys_enc))
            if keys.get(provider):
                return keys[provider]
        except Exception:
            pass
    return _SHARED_LLM_KEYS.get(provider, "")


def _guest_llm_client(model_id: str):
    """Like _user_llm_client but for open-mode/guest requests (no per-user key).

    Routes directly to the detected provider using the shared key.
    Raises LookupError when no shared key is configured for the provider.
    """
    provider = _detect_provider(model_id)
    base_url = _PROVIDER_URLS[provider]
    bare_model = model_id.split("/", 1)[1] if model_id.startswith(f"{provider}/") else model_id

    shared_key = _SHARED_LLM_KEYS.get(provider, "")
    if not shared_key:
        raise LookupError(f"No API key configured for provider '{provider}' — ask an admin to add one in Settings → LLM Keys")
    logger.info("Direct %s call (shared key, guest) for model %s", provider, model_id)
    return AsyncOpenAI(api_key=shared_key, base_url=base_url), bare_model


def _user_llm_client(user: User, model_id: str):
    """Returns (AsyncOpenAI client, effective_model_id) for a direct provider call.

    Priority:
    1. Per-user provider key → direct call to provider
    2. Shared admin/env key  → direct call to provider

    Raises LookupError when no key is configured for the model's provider.
    """
    provider = _detect_provider(model_id)
    base_url = _PROVIDER_URLS[provider]
    # Strip "provider/" namespace prefix for the actual API call
    bare_model = model_id.split("/", 1)[1] if model_id.startswith(f"{provider}/") else model_id

    # 1. Per-user key
    if user.llm_api_keys_enc:
        try:
            keys = json.loads(decrypt(user.llm_api_keys_enc))
            key = keys.get(provider, "")
            if key:
                logger.info("Using per-user %s key for %s", provider, user.email)
                return AsyncOpenAI(api_key=key, base_url=base_url), bare_model
        except Exception:
            pass

    # 2. Shared key
    shared_key = _SHARED_LLM_KEYS.get(provider, "")
    if shared_key:
        logger.info("Direct %s call (shared key) for model %s", provider, model_id)
        return AsyncOpenAI(api_key=shared_key, base_url=base_url), bare_model

    raise LookupError(f"No API key configured for provider '{provider}' — add one in Settings → LLM Keys")
