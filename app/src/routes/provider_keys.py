"""Shared LLM provider key management routes."""
import asyncio
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin
from crypto import decrypt, encrypt
from database import get_db
from models import AppSetting, User
from src.core.config import KNOWN_PROVIDERS, _SHARED_LLM_KEYS
from src.llm import catalog, discovery
from src.services.audit import _log_audit

logger = logging.getLogger(__name__)

router = APIRouter()

# ── LLM Provider Key management ──────────────────────────────────────────────

@router.get("/admin/llm-provider-keys")
async def list_llm_provider_keys(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    out = []
    for p in KNOWN_PROVIDERS:
        row = await db.get(AppSetting, f"provider_key_{p['id']}")
        key_preview = ""
        if row:
            try:
                raw = decrypt(row.value)
                key_preview = f"…{raw[-4:]}" if len(raw) >= 4 else "set"
            except Exception:
                key_preview = "set"
        # Strip provider-namespace prefix for display (e.g. "openai/gpt-4.1" → "gpt-4.1")
        disc = catalog._DISCOVERED_MODELS.get(p["id"], [])
        disc_bare = [m.split("/", 1)[-1] for m in disc]
        out.append({
            "provider": p["id"],
            "name": p["name"],
            "key_set": bool(row),
            "key_preview": key_preview,
            "discovered_models": disc_bare,
        })
    return out


class LLMProviderKeyUpdate(BaseModel):
    provider: str
    key: str


@router.post("/admin/llm-provider-keys")
async def set_llm_provider_key(
    body: LLMProviderKeyUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    valid_ids = {p["id"] for p in KNOWN_PROVIDERS}
    if body.provider not in valid_ids:
        raise HTTPException(status_code=422, detail=f"Unknown provider '{body.provider}'")
    if not body.key.strip():
        raise HTTPException(status_code=422, detail="Key cannot be empty")

    setting_key = f"provider_key_{body.provider}"
    enc = encrypt(body.key.strip())
    existing = await db.get(AppSetting, setting_key)
    if existing:
        existing.value = enc
    else:
        db.add(AppSetting(key=setting_key, value=enc))
    await db.commit()

    _SHARED_LLM_KEYS[body.provider] = body.key.strip()
    asyncio.create_task(discovery._run_discovery(body.provider, body.key.strip()))
    await _log_audit(db, admin.id, admin.email, "llm_provider_key_changed",
                     f"Provider key updated: {body.provider}")
    await db.commit()
    return {"ok": True}


@router.delete("/admin/llm-provider-keys/{provider}")
async def delete_llm_provider_key(
    provider: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    valid_ids = {p["id"] for p in KNOWN_PROVIDERS}
    if provider not in valid_ids:
        raise HTTPException(status_code=422, detail=f"Unknown provider '{provider}'")

    setting_key = f"provider_key_{provider}"
    existing = await db.get(AppSetting, setting_key)
    if existing:
        await db.delete(existing)
        await db.commit()

    _SHARED_LLM_KEYS[provider] = os.getenv(
        next(p["env"] for p in KNOWN_PROVIDERS if p["id"] == provider), ""
    )
    if provider in catalog._DISCOVERED_MODELS:
        del catalog._DISCOVERED_MODELS[provider]
        try:
            dm_row = await db.get(AppSetting, "discovered_models")
            if dm_row:
                dm_row.value = json.dumps(catalog._DISCOVERED_MODELS)
                await db.commit()
        except Exception as exc:
            logger.warning("Could not update discovered models after key removal: %s", exc)
        asyncio.create_task(catalog.refresh_model_cache())
    await _log_audit(db, admin.id, admin.email, "llm_provider_key_removed",
                     f"Provider key removed: {provider}")
    await db.commit()
    return {"ok": True}
