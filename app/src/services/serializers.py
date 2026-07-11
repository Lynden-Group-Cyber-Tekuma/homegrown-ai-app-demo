"""Response-shape helpers converting ORM rows / chat messages to API types."""
import json

from crypto import decrypt
from models import APIKey, User
from schemas import APIKeyOut, ChatMessage, PSTenantOut, UserOut
from src.core.config import SHOW_LLM_KEY_SETTINGS

def _user_out(user: User) -> UserOut:
    llm_providers: list[str] = []
    if user.llm_api_keys_enc:
        try:
            llm_providers = list(json.loads(decrypt(user.llm_api_keys_enc)).keys())
        except Exception:
            pass
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        daily_message_limit=user.daily_message_limit,
        allowed_models=user.allowed_models,
        ps_tenant_id=user.ps_tenant_id,
        ps_tenant=PSTenantOut.model_validate(user.ps_tenant) if user.ps_tenant else None,
        ps_configured=bool(user.ps_tenant_id and user.ps_api_key_enc),
        ps_mode=user.ps_mode,
        ps_enabled=user.ps_enabled,
        llm_key_settings_visible=SHOW_LLM_KEY_SETTINGS,
        llm_keys_configured=llm_providers,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
    )


def _api_key_out(key: APIKey) -> APIKeyOut:
    return APIKeyOut(
        id=key.id,
        name=key.name,
        key_preview=f"{key.key_prefix}…",
        is_active=key.is_active,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
    )


def _build_content(m: ChatMessage):
    if m.image_url:
        parts = [{"type": "image_url", "image_url": {"url": m.image_url}}]
        if m.content:
            parts.append({"type": "text", "text": m.content})
        return parts
    return m.content
