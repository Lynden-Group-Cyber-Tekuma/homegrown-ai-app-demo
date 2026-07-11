"""Application settings routes: general, security keys, and limits."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import create_access_token, hash_password, require_admin, reset_secret_key, set_secret_key
from crypto import (
    clear_encryption_key_override, encrypt, encryption_key_overridden,
    set_encryption_key, validate_fernet_key, write_encryption_key_override,
)
from database import get_db
from models import AppSetting, User
from src.core import config
from src.core.config import SMTP_HOST
from src.services.audit import _log_audit

logger = logging.getLogger(__name__)

router = APIRouter()

# ── App settings ─────────────────────────────────────────────────────────────

def _parse_app_settings(s: dict) -> dict:
    try:
        domains = json.loads(s.get("allowed_email_domains", "[]"))
        if not isinstance(domains, list):
            domains = []
    except Exception:
        domains = []
    try:
        smtp_port_val = int(s.get("smtp_port", "587") or "587")
    except (ValueError, TypeError):
        smtp_port_val = 587
    return {
        "user_mgmt_enabled": s.get("user_mgmt_enabled", "true") != "false",
        "fixed_model_enabled": s.get("fixed_model_enabled", "false") == "true",
        "fixed_model_id": s.get("fixed_model_id", None),
        "allowed_email_domains": domains,
        # Email settings
        "smtp_host": s.get("smtp_host", ""),
        "smtp_port": smtp_port_val,
        "email_username": s.get("email_username", ""),
        "email_password_set": bool(s.get("email_password_enc")),
        "from_email": s.get("from_email", ""),
        "jwt_secret_set": bool(s.get("jwt_secret_enc")),
        "admin_password_set": bool(s.get("admin_password_hash")),
        # Application settings
        "daily_limit": int(s["daily_limit"]) if s.get("daily_limit") else config.DEFAULT_DAILY_LIMIT,
        "max_file_mb": int(s["max_file_mb"]) if s.get("max_file_mb") else config.MAX_FILE_SIZE_MB,
        "wizard_completed": s.get("wizard_completed") == "true",
        "scenarios_sync_url": s.get("scenarios_sync_url", "https://raw.githubusercontent.com/prompt-security/homegrown-ai-app-demo/main/app/data/scenarios.json"),
        "scenarios_sync_branch": s.get("scenarios_sync_branch", "main"),
    }


@router.get("/app/settings")
async def get_app_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    parsed = _parse_app_settings(s)
    # True if SMTP will actually work — DB value takes precedence, env var is fallback
    parsed["smtp_configured"] = bool(parsed.get("smtp_host") or SMTP_HOST)
    parsed["encryption_key_set"] = encryption_key_overridden()
    return parsed


@router.patch("/admin/app-settings")
async def update_app_settings(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Keys that should be stored as-is (no lowercasing)
    _plain_str_keys = {"smtp_host", "smtp_port", "email_username", "from_email", "scenarios_sync_url", "scenarios_sync_branch"}
    for key, value in body.items():
        if key == "allowed_email_domains":
            # Store as a JSON array; validate it's a list of strings
            if not isinstance(value, list):
                raise HTTPException(status_code=422, detail="allowed_email_domains must be a list")
            serialised = json.dumps([str(d).lower().strip() for d in value if str(d).strip()])
            store_key = key
        elif key == "email_password":
            serialised = encrypt(str(value))
            store_key = "email_password_enc"
        elif key in _plain_str_keys:
            serialised = str(value)
            store_key = key
        else:
            serialised = str(value).lower()
            store_key = key
        existing = await db.get(AppSetting, store_key)
        if existing:
            existing.value = serialised
        else:
            db.add(AppSetting(key=store_key, value=serialised))
    await db.commit()
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    return _parse_app_settings(s)


class JwtSecretUpdate(BaseModel):
    secret: str


@router.post("/admin/jwt-secret")
async def update_jwt_secret(
    body: JwtSecretUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Store a new JWT signing secret, hot-swap it in-process, and persist to DB. Invalidates all active sessions."""
    if not body.secret or len(body.secret) < 32:
        raise HTTPException(status_code=422, detail="Secret must be at least 32 characters")

    enc = encrypt(body.secret)
    existing = await db.get(AppSetting, "jwt_secret_enc")
    if existing:
        existing.value = enc
    else:
        db.add(AppSetting(key="jwt_secret_enc", value=enc))
    await db.commit()

    await _log_audit(db, admin.id, admin.email, "jwt_secret_changed", "JWT signing secret updated via admin UI")
    await db.commit()

    # Hot-swap the key THEN immediately issue a new token signed with it so the
    # calling admin session survives — without this the next request would 401.
    set_secret_key(body.secret)
    new_token = create_access_token({"sub": str(admin.id)})
    return {"ok": True, "token": new_token}


class EncryptionKeyUpdate(BaseModel):
    key: str


@router.post("/admin/encryption-key")
async def update_encryption_key(
    body: EncryptionKeyUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Persist a new Fernet encryption key and hot-swap it in-process.
    WARNING: any data encrypted with the old key (PS API keys, email password, etc.) will no longer be readable."""
    if not body.key:
        raise HTTPException(status_code=422, detail="Key cannot be empty")
    if not validate_fernet_key(body.key):
        raise HTTPException(status_code=422, detail="Invalid Fernet key — must be a URL-safe base64-encoded 32-byte value")

    try:
        write_encryption_key_override(body.key)
    except OSError as exc:
        logger.error("Failed to write encryption key override file: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Could not persist key to disk: {exc.strerror} ({exc.filename}). "
                   "Ensure the app/data/ directory is writable by the container user.",
        )

    set_encryption_key(body.key)

    await _log_audit(db, admin.id, admin.email, "encryption_key_changed", "Fernet encryption key updated via admin UI")
    await db.commit()

    return {"ok": True}


class AdminPasswordUpdate(BaseModel):
    password: str
    confirm: str
    email: Optional[str] = None


@router.post("/admin/admin-password")
async def update_admin_password(
    body: AdminPasswordUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set the admin password. Optionally update the admin email (used by the setup wizard)."""
    if not body.password:
        raise HTTPException(status_code=422, detail="Password cannot be empty")
    if body.password != body.confirm:
        raise HTTPException(status_code=422, detail="Passwords do not match")

    if body.email:
        new_email = body.email.strip().lower()
        if not new_email or "@" not in new_email:
            raise HTTPException(status_code=422, detail="Invalid email address")
        conflict = await db.scalar(
            select(User).where(User.email == new_email, User.id != admin.id)
        )
        if conflict:
            raise HTTPException(status_code=409, detail="Email already in use")
        admin.email = new_email
        admin.must_change_password = False

    hashed = hash_password(body.password)
    existing = await db.get(AppSetting, "admin_password_hash")
    if existing:
        existing.value = hashed
    else:
        db.add(AppSetting(key="admin_password_hash", value=hashed))
    await db.commit()

    await _log_audit(db, admin.id, admin.email, "admin_password_changed", "Admin password updated")
    await db.commit()
    return {"ok": True}


@router.delete("/admin/jwt-secret")
async def clear_jwt_secret(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove the stored JWT secret and revert to the env-var value (ephemeral in dev)."""
    row = await db.get(AppSetting, "jwt_secret_enc")
    if row:
        await db.delete(row)
        await db.commit()
    reset_secret_key()
    await _log_audit(db, admin.id, admin.email, "jwt_secret_cleared", "JWT secret cleared via admin UI — reverted to env default")
    await db.commit()
    return {"ok": True}


@router.delete("/admin/encryption-key")
async def clear_encryption_key(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove the stored encryption key override and revert to the env-var value (or new ephemeral key)."""
    clear_encryption_key_override()
    await _log_audit(db, admin.id, admin.email, "encryption_key_cleared", "Encryption key override cleared via admin UI — reverted to env default")
    await db.commit()
    return {"ok": True}


class ApplicationSettingsUpdate(BaseModel):
    daily_limit: int | None = None
    max_file_mb: int | None = None


@router.patch("/admin/application-settings")
async def update_application_settings(
    body: ApplicationSettingsUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):

    async def _upsert(key: str, value: str) -> None:
        existing = await db.get(AppSetting, key)
        if existing:
            existing.value = value
        else:
            db.add(AppSetting(key=key, value=value))

    if body.daily_limit is not None:
        if body.daily_limit < 0:
            raise HTTPException(status_code=422, detail="daily_limit must be 0 or greater (0 = unlimited)")
        await _upsert("daily_limit", str(body.daily_limit))
        config.DEFAULT_DAILY_LIMIT = body.daily_limit or None

    if body.max_file_mb is not None:
        if body.max_file_mb < 1:
            raise HTTPException(status_code=422, detail="max_file_mb must be at least 1")
        await _upsert("max_file_mb", str(body.max_file_mb))
        config.MAX_FILE_SIZE_MB = body.max_file_mb
        config.MAX_FILE_SIZE_BYTES = config.MAX_FILE_SIZE_MB * 1024 * 1024

    await db.commit()
    await _log_audit(db, admin.id, admin.email, "application_settings_changed", "Application settings updated via admin UI")
    await db.commit()
    return {"ok": True}
