"""Audit-event and chat-message persistence helpers."""
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models import AuditEvent, Message

async def _log_audit(
    db: AsyncSession,
    user_id: Optional[int],
    user_email: str,
    event_type: str,
    detail: Optional[str] = None,
):
    ev = AuditEvent(user_id=user_id, user_email=user_email, event_type=event_type, detail=detail)
    db.add(ev)
    await db.commit()


async def _log_msg(
    db: AsyncSession,
    session_id: str,
    user_id: Optional[int],
    role: str,
    content: str,
    model: Optional[str],
    ps_scanned: bool = False,
    ps_blocked: bool = False,
    ps_action: str = "pass",
    ps_violations: Optional[list] = None,
    response_ms: Optional[int] = None,
    guest_id: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
):
    msg = Message(
        session_id=session_id,
        user_id=user_id,
        guest_id=guest_id,
        role=role,
        content=content,
        model=model,
        ps_scanned=ps_scanned,
        ps_action=ps_action if not ps_blocked else "block",
        ps_violations=ps_violations or [],
        response_ms=response_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    db.add(msg)
    await db.commit()
