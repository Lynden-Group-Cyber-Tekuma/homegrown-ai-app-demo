"""Admin activity log and database reset routes."""
from fastapi import APIRouter, Depends
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth import hash_password, require_admin, reset_secret_key
from crypto import clear_encryption_key_override
from database import get_db
from models import AuditEvent, Message, User
from src.services.scenarios_seed import _seed_demo_scenarios

router = APIRouter()

# ── Admin: Activity log ───────────────────────────────────────────────────────
@router.get("/admin/activity")
async def admin_activity(
    limit: int = 100,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    msg_result = await db.execute(
        select(Message, User.email)
        .outerjoin(User, User.id == Message.user_id)
        .order_by(desc(Message.created_at))
        .limit(min(limit, 500))
    )
    chat_rows = [
        {
            "id": f"msg-{msg.id}",
            "entry_type": "chat",
            "user_email": email or msg.guest_id or "guest",
            "session_id": msg.session_id,
            "role": msg.role,
            "content_preview": msg.content[:120] + ("…" if len(msg.content) > 120 else ""),
            "model": msg.model,
            "ps_scanned": msg.ps_scanned,
            "ps_action": msg.ps_action,
            "ps_violations": msg.ps_violations,
            "response_ms": msg.response_ms,
            "created_at": msg.created_at.isoformat(),
        }
        for msg, email in msg_result.all()
    ]

    audit_result = await db.execute(
        select(AuditEvent)
        .order_by(desc(AuditEvent.created_at))
        .limit(min(limit, 200))
    )
    audit_rows = [
        {
            "id": f"audit-{ev.id}",
            "entry_type": "audit",
            "user_email": ev.user_email,
            "role": "system",
            "event_type": ev.event_type,
            "content_preview": ev.detail or ev.event_type,
            "detail": ev.detail,
            "model": None,
            "ps_scanned": False,
            "ps_action": None,
            "created_at": ev.created_at.isoformat(),
        }
        for ev in audit_result.scalars().all()
    ]

    combined = sorted(chat_rows + audit_rows, key=lambda r: r["created_at"], reverse=True)
    return combined[:min(limit, 500)]


@router.delete("/admin/activity", status_code=204)
async def clear_activity(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(text("DELETE FROM audit_events"))
    await db.execute(text("DELETE FROM messages"))
    await db.execute(text("DELETE FROM chat_sessions"))
    await db.commit()


@router.post("/admin/reset-database", status_code=204)
async def reset_database(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Wipe all data and settings, then re-seed the default admin user and demo scenarios."""
    for table in [
        "audit_events", "messages", "chat_sessions",
        "api_keys", "users", "ps_tenants", "demo_scenarios", "app_settings",
    ]:
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()

    # Re-create a bootstrap admin placeholder so the wizard can be completed again
    import secrets as _secrets
    new_admin = User(
        email=f"setup-{_secrets.token_hex(8)}@wizard.internal",
        hashed_password=hash_password(_secrets.token_hex(32)),
        role="admin",
        is_active=True,
        must_change_password=True,
    )
    db.add(new_admin)
    await db.commit()

    await _seed_demo_scenarios(db)

    # Clear the on-disk encryption key override file so the wizard starts clean
    clear_encryption_key_override()
    # Reset in-memory JWT to ephemeral so setup wizard can re-issue a bootstrap token
    reset_secret_key()
