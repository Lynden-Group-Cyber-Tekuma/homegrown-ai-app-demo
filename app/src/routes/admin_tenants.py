"""Admin PS tenant management routes (plus the public read used by settings)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_admin
from database import get_db
from models import PSTenant, User
from schemas import PSTenantCreate, PSTenantOut, PSTenantUpdate
from src.services.audit import _log_audit

router = APIRouter()

# ── Admin: PS Tenants ─────────────────────────────────────────────────────────
@router.get("/admin/ps-tenants", response_model=list[PSTenantOut])
async def admin_list_ps_tenants(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PSTenant).order_by(PSTenant.name))
    return result.scalars().all()


@router.post("/admin/ps-tenants", response_model=PSTenantOut, status_code=201)
async def admin_create_ps_tenant(
    body: PSTenantCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.scalar(select(PSTenant).where(PSTenant.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail="Tenant name already exists")
    tenant = PSTenant(name=body.name, base_url=body.base_url, gateway_url=body.gateway_url)
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    await _log_audit(db, admin.id, admin.email, "tenant_created", f"{tenant.name} — {tenant.base_url}")
    return tenant


@router.patch("/admin/ps-tenants/{tenant_id}", response_model=PSTenantOut)
async def admin_update_ps_tenant(
    tenant_id: int,
    body: PSTenantUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(PSTenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if body.name is not None:
        tenant.name = body.name
    if body.base_url is not None:
        tenant.base_url = body.base_url
    if body.gateway_url is not None:
        tenant.gateway_url = body.gateway_url
    await db.commit()
    await db.refresh(tenant)
    await _log_audit(db, admin.id, admin.email, "tenant_updated", f"{tenant.name}")
    return tenant


@router.delete("/admin/ps-tenants/{tenant_id}", status_code=204)
async def admin_delete_ps_tenant(
    tenant_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(PSTenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    name = tenant.name
    await db.delete(tenant)
    await db.commit()
    await _log_audit(db, admin.id, admin.email, "tenant_deleted", f"name={name}")


# ── Admin: PS Tenants (non-admin read — for settings dropdown) ────────────────
@router.get("/ps-tenants", response_model=list[PSTenantOut])
async def list_ps_tenants_public(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PSTenant).order_by(PSTenant.name))
    return result.scalars().all()
