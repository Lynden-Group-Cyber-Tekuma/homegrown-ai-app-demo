"""User self-service routes: stats, PS config, and per-user LLM keys."""
import json
from datetime import datetime, timezone

import httpx
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from crypto import decrypt, encrypt
from database import get_db
from models import Message, PSTenant, User
from schemas import LLMKeysUpdate, PSConfigUpdate, UserOut, UserStats
from src.services.audit import _log_audit
from src.services.serializers import _user_out

logger = logging.getLogger(__name__)

router = APIRouter()

# ── User self-service ─────────────────────────────────────────────────────────
@router.get("/users/me/stats", response_model=UserStats)
async def my_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    count = await db.scalar(
        select(func.count(Message.id)).where(
            Message.user_id == current_user.id,
            Message.role == "user",
            Message.created_at >= today_start,
        )
    ) or 0
    return UserStats(messages_today=count, daily_limit=current_user.daily_message_limit)


@router.patch("/users/me/ps-config", response_model=UserOut)
async def update_my_ps_config(
    body: PSConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.ps_tenant_id is not None:
        tenant = await db.get(PSTenant, body.ps_tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="PS Tenant not found")
        if current_user.ps_tenant_id != body.ps_tenant_id and body.ps_api_key is None:
            # Tenant changed without a new App ID — clear the stored key so the
            # old tenant's App ID is not silently used against the new endpoint.
            current_user.ps_api_key_enc = None
        current_user.ps_tenant_id = body.ps_tenant_id

    if body.ps_api_key is not None:
        current_user.ps_api_key_enc = encrypt(body.ps_api_key) if body.ps_api_key else None

    if body.ps_mode in ("api", "gateway"):
        current_user.ps_mode = body.ps_mode

    if body.ps_enabled is not None:
        current_user.ps_enabled = body.ps_enabled

    await db.commit()
    await db.refresh(current_user, ["ps_tenant"])
    parts = []
    if body.ps_api_key is not None: parts.append("App ID updated")
    if body.ps_mode in ("api", "gateway"): parts.append(f"mode={body.ps_mode}")
    if body.ps_enabled is not None: parts.append(f"enabled={body.ps_enabled}")
    if body.ps_tenant_id is not None: parts.append(f"tenant_id={body.ps_tenant_id}")
    await _log_audit(db, current_user.id, current_user.email, "ps_config_changed", "; ".join(parts) or None)
    return _user_out(current_user)



# ── User: LLM key overrides ───────────────────────────────────────────────────
@router.patch("/users/me/llm-keys", response_model=UserOut)
async def update_my_llm_keys(
    body: LLMKeysUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing: dict = {}
    if current_user.llm_api_keys_enc:
        try:
            existing = json.loads(decrypt(current_user.llm_api_keys_enc))
        except Exception:
            pass

    for provider in ("openai", "anthropic", "google", "openrouter"):
        val = getattr(body, provider)
        if val is None:
            continue
        if val.strip():
            existing[provider] = val.strip()
        else:
            existing.pop(provider, None)

    current_user.llm_api_keys_enc = encrypt(json.dumps(existing)) if existing else None
    await db.commit()
    await db.refresh(current_user, ["ps_tenant"])
    updated = [p for p in ("openai","anthropic","google","openrouter") if getattr(body, p) is not None]
    await _log_audit(db, current_user.id, current_user.email, "llm_keys_updated", "providers: " + ", ".join(updated))
    return _user_out(current_user)


# ── User: Validate LLM API key ──────────────────────────────────────────────
@router.post("/users/me/validate-llm-key")
async def validate_llm_key(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    body = await request.json()
    provider = body.get("provider", "")
    api_key = body.get("api_key", "")
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key are required")

    urls = {
        "openai": "https://api.openai.com/v1/models",
        "google": "https://generativelanguage.googleapis.com/v1beta/models",
        "perplexity": "https://api.perplexity.ai/models",
        "openrouter": "https://openrouter.ai/api/v1/models",
    }

    if provider == "anthropic":
        # Anthropic has no /models endpoint; verify with a small messages call
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    json={"model": "claude-sonnet-4-5-20250929", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                )
                if resp.status_code in (200, 201):
                    return {"valid": True, "provider": provider}
                elif resp.status_code == 401:
                    return {"valid": False, "provider": provider, "error": "Invalid API key"}
                else:
                    return {"valid": True, "provider": provider}  # non-401 likely means key is valid but other issue
        except Exception as e:
            logger.warning("LLM key validation error (%s): %s", provider, e)
            return {"valid": False, "provider": provider, "error": "Could not reach the provider. Check your network connection."}

    url = urls.get(provider)
    if not url:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                model_count = len(data.get("data", data.get("models", [])))
                return {"valid": True, "provider": provider, "models_count": model_count}
            elif resp.status_code == 401:
                return {"valid": False, "provider": provider, "error": "Invalid API key"}
            else:
                return {"valid": False, "provider": provider, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        logger.warning("LLM key validation error (%s): %s", provider, e)
        return {"valid": False, "provider": provider, "error": "Could not reach the provider. Check your network connection."}
