"""Model catalogue: discovered-model store, fallback list, and cached model list."""
import logging

logger = logging.getLogger(__name__)

_DISCOVERED_MODELS: dict[str, list[str]] = {}  # provider → [prefixed model IDs like "openai/gpt-4.1"]


# ── Fallback model list (used when provider discovery hasn't run yet) ────────
# These route directly to the provider detected from the model ID and only
# appear in the picker when a key for that provider is configured.
_FALLBACK_MODELS = [
    {"id": "gpt-4o"},
    {"id": "gpt-4o-mini"},
    {"id": "claude-sonnet-4-5-20250929"},
    {"id": "gemini-2.0-flash"},
    {"id": "gemini-1.5-pro"},
    {"id": "meta-llama/llama-3.1-8b-instruct:free"},
    {"id": "nvidia/nemotron-nano-9b-v2:free"},
    {"id": "mistralai/mistral-7b-instruct:free"},
]

# ── Cached model list ─────────────────────────────────────────────────────────
_model_cache: list[dict] = []


async def refresh_model_cache() -> list[dict]:
    """Rebuild the model list from provider discovery results (deduplicated by bare name)."""
    global _model_cache
    _model_cache = []
    existing_ids: set[str] = set()
    existing_bare: set[str] = set()
    for provider_models in _DISCOVERED_MODELS.values():
        for mid in provider_models:
            bare = mid.split("/", 1)[-1]
            if mid not in existing_ids and bare not in existing_bare:
                _model_cache.append({"id": mid})
                existing_ids.add(mid)
                existing_bare.add(bare)
    if _model_cache:
        logger.info("Model cache: %d discovered model(s)", len(_model_cache))
    return _model_cache
