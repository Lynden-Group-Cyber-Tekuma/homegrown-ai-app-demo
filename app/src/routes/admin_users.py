"""Admin user management routes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import hash_password, require_admin
from database import get_db
from models import User
from schemas import UserCreate, UserOut, UserUpdate
from src.core import config
from src.services.audit import _log_audit
from src.services.serializers import _user_out

router = APIRouter()

# ── Admin: Users ──────────────────────────────────────────────────────────────
@router.get("/admin/users", response_model=list[UserOut])
async def admin_list_users(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(User.role != "admin").options(selectinload(User.ps_tenant)).order_by(User.created_at)
    )
    return [_user_out(u) for u in result.scalars().all()]


@router.post("/admin/users", response_model=UserOut, status_code=201)
async def admin_create_user(
    body: UserCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        role=body.role,
        daily_message_limit=body.daily_message_limit if body.daily_message_limit is not None else config.DEFAULT_DAILY_LIMIT,
        allowed_models=body.allowed_models,
        ps_tenant_id=body.ps_tenant_id,
        must_change_password=body.must_change_password,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user, ["ps_tenant"])
    await _log_audit(db, admin.id, admin.email, "user_created", f"email={user.email} role={user.role} must_change_password={body.must_change_password}")
    return _user_out(user)


@router.get("/admin/users/{user_id}", response_model=UserOut)
async def admin_get_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_out(user)


@router.patch("/admin/users/{user_id}", response_model=UserOut)
async def admin_update_user(
    user_id: int,
    body: UserUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.email is not None:
        user.email = body.email
    if body.password is not None:
        user.hashed_password = hash_password(body.password)
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.daily_message_limit is not None:
        user.daily_message_limit = body.daily_message_limit
    if body.allowed_models is not None:
        user.allowed_models = body.allowed_models
    if body.ps_tenant_id is not None:
        user.ps_tenant_id = body.ps_tenant_id
    if body.ps_enabled is not None:
        user.ps_enabled = body.ps_enabled
    if body.must_change_password is not None:
        user.must_change_password = body.must_change_password

    await db.commit()
    await db.refresh(user, ["ps_tenant"])
    changed = [k for k in ("email","role","is_active","ps_enabled","ps_tenant_id","must_change_password") if getattr(body,k,None) is not None]
    await _log_audit(db, admin.id, admin.email, "user_updated", f"{user.email}: {', '.join(changed)}")
    return _user_out(user)


@router.delete("/admin/users/{user_id}", status_code=204)
async def admin_delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    email = user.email
    await db.delete(user)
    await db.commit()
    await _log_audit(db, admin.id, admin.email, "user_deleted", f"email={email}")
