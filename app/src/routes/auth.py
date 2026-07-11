"""Authentication, first-run setup, and user API key routes."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import (
    TOKEN_TTL_H, create_access_token, create_api_key, get_current_user,
    hash_api_key, hash_password, verify_password,
)
from database import get_db
from models import APIKey, AppSetting, AuditEvent, User
from schemas import APIKeyCreateRequest, APIKeyCreateResponse, APIKeyOut, LoginRequest, TokenResponse, UserOut
from src.core.config import ADMIN_PASSWORD, KNOWN_PROVIDERS
from src.services.audit import _log_audit
from src.services.serializers import _api_key_out, _user_out

router = APIRouter()

# ── Auth ──────────────────────────────────────────────────────────────────────
@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.email == body.email, User.is_active == True)
        .options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Detect first-ever login by checking for prior login events
    prior_logins = await db.scalar(
        select(func.count(AuditEvent.id))
        .where(AuditEvent.user_id == user.id, AuditEvent.event_type == "user_login")
    )
    if prior_logins == 0:
        await _log_audit(db, user.id, user.email, "user_first_login", "First sign-in")
    await _log_audit(db, user.id, user.email, "user_login", None)

    token = create_access_token({"sub": str(user.id)})
    # httpOnly cookie used for server-side page-level auth (e.g. GET /admin)
    response.set_cookie(
        "hgapp_session", token,
        httponly=True, samesite="lax", secure=False,
        max_age=TOKEN_TTL_H * 3600,
    )
    return TokenResponse(
        access_token=token,
        user=_user_out(user),
    )


@router.post("/auth/admin-login", response_model=TokenResponse)
async def admin_login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    """Password-only admin login. Checks against the stored admin_password_hash (falls back to ADMIN_PASSWORD env var)."""
    # Find the single admin user
    result = await db.execute(
        select(User)
        .where(User.role == "admin", User.is_active == True)
        .options(selectinload(User.ps_tenant))
    )
    admin_user = result.scalar_one_or_none()
    if not admin_user:
        raise HTTPException(status_code=401, detail="Invalid password")

    # Check against AppSetting hash first, fall back to env var
    pw_row = await db.get(AppSetting, "admin_password_hash")
    if pw_row:
        if not verify_password(body.password, pw_row.value):
            raise HTTPException(status_code=401, detail="Invalid password")
    else:
        if body.password != ADMIN_PASSWORD:
            raise HTTPException(status_code=401, detail="Invalid password")

    await _log_audit(db, admin_user.id, admin_user.email, "user_login", "Admin password-only login")
    await db.commit()

    token = create_access_token({"sub": str(admin_user.id)})
    response.set_cookie(
        "hgapp_session", token,
        httponly=True, samesite="lax", secure=False,
        max_age=TOKEN_TTL_H * 3600,
    )
    return TokenResponse(access_token=token, user=_user_out(admin_user))


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


@router.post("/auth/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.hashed_password or not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=422, detail="New password must differ from the current password")

    current_user.hashed_password = hash_password(body.new_password)
    current_user.must_change_password = False
    await db.commit()
    await _log_audit(db, current_user.id, current_user.email, "password_changed", "User changed password on first login")
    await db.commit()
    return {"ok": True}


@router.post("/auth/logout", status_code=204)
async def logout(response: Response):
    """Clear the server-side session cookie."""
    response.delete_cookie("hgapp_session", samesite="lax")
    return

@router.get("/auth/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return _user_out(current_user)


def _setup_complete(s: dict) -> bool:
    """Return True only when all required items are configured.

    LLM source: at least one provider key.
    Encryption key override is advisory (ephemeral fallback works); excluded here
    so a missing override file doesn't trap admins in a login→admin redirect loop.
    """
    any_llm = any(s.get(f"provider_key_{p['id']}") for p in KNOWN_PROVIDERS)
    return bool(
        s.get("admin_password_hash")
        and s.get("jwt_secret_enc")
        and any_llm
    )


@router.get("/setup/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    """No-auth endpoint. Returns whether initial setup is still needed and if default credentials are still active."""
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    admin = await db.scalar(select(User).where(User.role == "admin", User.is_active == True))
    default_credentials = bool(admin and admin.must_change_password)
    return {"needs_setup": not _setup_complete(s), "default_credentials": default_credentials}


@router.post("/setup/bootstrap-token")
async def setup_bootstrap_token(db: AsyncSession = Depends(get_db)):
    """No-auth endpoint. Issues a short-lived admin JWT for the Setup Wizard.
    Remains available until all required wizard items are configured.
    Returns 403 once setup is complete."""
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    if _setup_complete(s):
        raise HTTPException(status_code=403, detail="Setup already complete")
    admin = await db.scalar(select(User).where(User.role == "admin", User.is_active == True))
    if not admin:
        raise HTTPException(status_code=500, detail="No admin user found")
    token = create_access_token({"sub": str(admin.id)}, expires_h=2)
    return {"access_token": token}


@router.get("/users/me/api-keys", response_model=list[APIKeyOut])
async def list_my_api_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == current_user.id)
        .order_by(desc(APIKey.created_at))
    )
    return [_api_key_out(k) for k in result.scalars().all()]


@router.post("/users/me/api-keys", response_model=APIKeyCreateResponse, status_code=201)
async def create_my_api_key(
    body: APIKeyCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Key name is required")

    raw_key = create_api_key()
    prefix = raw_key[:16]
    record = APIKey(
        user_id=current_user.id,
        name=name,
        key_prefix=prefix,
        key_hash=hash_api_key(raw_key),
        is_active=True,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    await _log_audit(db, current_user.id, current_user.email, "api_key_created", f"name={name}")
    return APIKeyCreateResponse(api_key=raw_key, key=_api_key_out(record))


@router.delete("/users/me/api-keys/{key_id}", status_code=204)
async def delete_my_api_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    key = await db.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == current_user.id)
    )
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(key)
    await db.commit()
    await _log_audit(db, current_user.id, current_user.email, "api_key_deleted", f"id={key_id} name={key.name}")
