"""Health, version, model list, and token estimation routes."""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user, require_admin
from crypto import decrypt
from models import User
from schemas import ChatRequest, TokenEstimateResponse
from src.core.config import _SHARED_LLM_KEYS
from src.llm import catalog, routing
from src.services.serializers import _build_content
from token_counter import estimate_message_tokens

router = APIRouter()

# ── Health + Models ───────────────────────────────────────────────────────────
@router.get("/health")
async def health():
    return {
        "status": "ok",
        "models_loaded": len(catalog._model_cache),
    }


_APP_VERSION: str = "unknown"

def _load_app_version() -> str:
    """Read VERSION file from repo root (one level above app/)."""
    _app_dir = Path(__file__).resolve().parents[2]  # app/
    for candidate in [
        _app_dir.parent / "VERSION",  # repo root when running in container
        _app_dir / "VERSION",         # fallback if copied alongside app
    ]:
        if candidate.exists():
            return candidate.read_text().strip()
    return "unknown"

_APP_VERSION = _load_app_version()


@router.get("/version")
async def version():
    return {"version": _APP_VERSION}

@router.get("/models")
async def models(current_user: User = Depends(get_current_user)):
    live = catalog._model_cache or await catalog.refresh_model_cache()
    available = live if live else catalog._FALLBACK_MODELS
    allowed = current_user.allowed_models
    if allowed is not None:
        available = [m for m in available if m["id"] in allowed]

    # Build set of providers that have a usable key (user-level or shared)
    user_providers: set[str] = set()
    if current_user.llm_api_keys_enc:
        try:
            user_providers = {k for k, v in json.loads(decrypt(current_user.llm_api_keys_enc)).items() if v}
        except Exception:
            pass
    shared_providers = {k for k, v in _SHARED_LLM_KEYS.items() if v}

    enriched = []
    for m in available:
        meta = routing._model_meta(m["id"])
        provider = routing._detect_provider(m["id"])
        key_set = provider in user_providers or provider in shared_providers
        enriched.append({**m, **meta, "key_set": key_set})

    return {"models": enriched, "fallback": not bool(live)}


@router.post("/admin/refresh-models")
async def admin_refresh_models(admin: User = Depends(require_admin)):
    updated = await catalog.refresh_model_cache()
    return {"models_loaded": len(updated), "fallback": not bool(updated)}


@router.post("/chat/token-estimate", response_model=TokenEstimateResponse)
async def chat_token_estimate(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    available = catalog._model_cache or catalog._FALLBACK_MODELS
    model = request.model or available[0]["id"]
    if not model:
        raise HTTPException(status_code=503, detail="No models available — save a provider API key in Admin → Settings → LLM Keys")

    if current_user.allowed_models is not None and model not in current_user.allowed_models:
        raise HTTPException(status_code=403, detail="Model not allowed for this user")

    system_prompt = request.system_prompt or "You are a helpful AI assistant."
    payload = [{"role": "system", "content": system_prompt}] + [
        {"role": m.role, "content": _build_content(m)} for m in request.messages
    ]
    return TokenEstimateResponse(
        estimated_prompt_tokens=estimate_message_tokens(payload, model=model),
        model=model,
    )
