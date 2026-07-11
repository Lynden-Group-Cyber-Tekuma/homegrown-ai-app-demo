"""Admin statistics and reporting routes."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin
from database import get_db
from models import AuditEvent, ChatSession, Message, User

router = APIRouter()

# ── Admin: Stats ──────────────────────────────────────────────────────────────
@router.get("/admin/stats")
async def admin_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    total_messages = await db.scalar(select(func.count(Message.id))) or 0
    messages_today = await db.scalar(
        select(func.count(Message.id)).where(Message.created_at >= today_start)
    ) or 0
    total_users = await db.scalar(select(func.count(User.id))) or 0
    total_sessions = await db.scalar(select(func.count(ChatSession.id))) or 0

    # Active users today
    active_result = await db.execute(
        select(func.count(func.distinct(Message.user_id)))
        .where(Message.created_at >= today_start)
    )
    active_users_today = active_result.scalar() or 0

    # PS actions distribution
    ps_actions_result = await db.execute(
        select(Message.ps_action, func.count(Message.id))
        .where(Message.ps_scanned == True)
        .group_by(Message.ps_action)
    )
    ps_actions = {row[0]: row[1] for row in ps_actions_result.all() if row[0]}
    ps_scanned = sum(ps_actions.values())
    ps_blocked  = ps_actions.get("block", 0)

    # Messages by day (last 14 days)
    from sqlalchemy import cast, Date as SADate
    days_result = await db.execute(
        select(cast(Message.created_at, SADate).label("day"), func.count(Message.id))
        .group_by("day")
        .order_by("day")
        .limit(14)
    )
    messages_by_day = [{"date": str(r[0]), "count": r[1]} for r in days_result.all()]

    # Messages by model
    model_result = await db.execute(
        select(Message.model, func.count(Message.id))
        .where(Message.model.isnot(None))
        .group_by(Message.model)
        .order_by(desc(func.count(Message.id)))
        .limit(8)
    )
    messages_by_model = {r[0]: r[1] for r in model_result.all()}

    # Top users (all time)
    top_users_result = await db.execute(
        select(User.id, User.email, func.count(Message.id).label("count"))
        .join(Message, Message.user_id == User.id)
        .where(Message.role == "user")
        .group_by(User.id, User.email)
        .order_by(desc("count"))
        .limit(10)
    )
    top_users = [{"id": r[0], "email": r[1], "message_count": r[2]} for r in top_users_result.all()]

    # Per-user message counts today
    user_counts_result = await db.execute(
        select(User.email, func.count(Message.id).label("count"))
        .join(Message, Message.user_id == User.id)
        .where(Message.created_at >= today_start, Message.role == "user")
        .group_by(User.email)
        .order_by(desc("count"))
    )
    user_counts = [{"email": r[0], "count": r[1]} for r in user_counts_result.all()]

    # Token usage totals
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_tokens_all = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.role == "assistant")
    ) or 0
    total_tokens_month = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.role == "assistant", Message.created_at >= month_start)
    ) or 0
    total_tokens_today = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.role == "assistant", Message.created_at >= today_start)
    ) or 0

    return {
        "total_messages": total_messages,
        "messages_today": messages_today,
        "total_users": total_users,
        "total_sessions": total_sessions,
        "active_users_today": active_users_today,
        "ps_actions": ps_actions,
        "ps_scanned": ps_scanned,
        "ps_blocked": ps_blocked,
        "messages_by_day": messages_by_day,
        "messages_by_model": messages_by_model,
        "top_users": top_users,
        "user_counts_today": user_counts,
        "total_tokens_all": int(total_tokens_all),
        "total_tokens_month": int(total_tokens_month),
        "total_tokens_today": int(total_tokens_today),
    }


@router.get("/admin/guest-activity")
async def admin_guest_activity(
    identifier: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Return sessions with their messages for rich chat history
    sessions_result = await db.execute(
        select(ChatSession)
        .where(ChatSession.guest_id == identifier)
        .order_by(desc(ChatSession.created_at))
        .limit(200)
    )
    sessions = sessions_result.scalars().all()
    session_ids = [s.id for s in sessions]
    session_map = {s.id: s for s in sessions}

    if session_ids:
        msgs_result = await db.execute(
            select(Message)
            .where(Message.session_id.in_(session_ids))
            .order_by(Message.session_id, Message.id)
        )
        messages = msgs_result.scalars().all()
    else:
        messages = []

    # Group messages by session
    from collections import defaultdict
    by_session: dict = defaultdict(list)
    for m in messages:
        by_session[m.session_id].append(m)

    result = []
    for sid in session_ids:
        s = session_map[sid]
        result.append({
            "session_id": sid,
            "title": s.title,
            "created_at": s.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "model": m.model,
                    "ps_scanned": m.ps_scanned,
                    "ps_action": m.ps_action,
                    "ps_violations": m.ps_violations or [],
                    "prompt_tokens": m.prompt_tokens,
                    "completion_tokens": m.completion_tokens,
                    "total_tokens": m.total_tokens,
                    "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for m in by_session[sid]
            ],
        })
    return result


@router.get("/admin/users/{user_id}/chat-history")
async def admin_user_chat_history(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    sessions_result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(desc(ChatSession.created_at))
        .limit(200)
    )
    sessions = sessions_result.scalars().all()
    session_ids = [s.id for s in sessions]
    session_map = {s.id: s for s in sessions}

    if session_ids:
        msgs_result = await db.execute(
            select(Message)
            .where(Message.session_id.in_(session_ids))
            .order_by(Message.session_id, Message.id)
        )
        messages = msgs_result.scalars().all()
    else:
        messages = []

    from collections import defaultdict
    by_session: dict = defaultdict(list)
    for m in messages:
        by_session[m.session_id].append(m)

    result = []
    for sid in session_ids:
        s = session_map[sid]
        result.append({
            "session_id": sid,
            "title": s.title,
            "created_at": s.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "model": m.model,
                    "ps_scanned": m.ps_scanned,
                    "ps_action": m.ps_action,
                    "ps_violations": m.ps_violations or [],
                    "prompt_tokens": m.prompt_tokens,
                    "completion_tokens": m.completion_tokens,
                    "total_tokens": m.total_tokens,
                    "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for m in by_session[sid]
            ],
        })
    return result


@router.get("/admin/guest-stats")
async def admin_guest_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import cast, Date as SADate
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_events = await db.scalar(
        select(func.count(AuditEvent.id)).where(AuditEvent.event_type == "guest_chat")
    ) or 0
    events_today = await db.scalar(
        select(func.count(AuditEvent.id))
        .where(AuditEvent.event_type == "guest_chat", AuditEvent.created_at >= today_start)
    ) or 0
    total_guests = await db.scalar(
        select(func.count(func.distinct(AuditEvent.user_email)))
        .where(AuditEvent.event_type == "guest_chat")
    ) or 0
    guests_today = await db.scalar(
        select(func.count(func.distinct(AuditEvent.user_email)))
        .where(AuditEvent.event_type == "guest_chat", AuditEvent.created_at >= today_start)
    ) or 0

    # Aggregate token totals across all guest messages
    total_tokens_all = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant")
    ) or 0
    total_tokens_month = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant",
               Message.created_at >= month_start)
    ) or 0

    days_result = await db.execute(
        select(cast(AuditEvent.created_at, SADate).label("day"), func.count(AuditEvent.id))
        .where(AuditEvent.event_type == "guest_chat")
        .group_by("day")
        .order_by("day")
        .limit(14)
    )
    events_by_day = [{"date": str(r[0]), "count": r[1]} for r in days_result.all()]

    top_guests_result = await db.execute(
        select(AuditEvent.user_email, func.count(AuditEvent.id).label("count"))
        .where(AuditEvent.event_type == "guest_chat")
        .group_by(AuditEvent.user_email)
        .order_by(desc("count"))
        .limit(50)
    )
    top_guests = [{"identifier": r[0], "count": r[1]} for r in top_guests_result.all()]

    last_seen_result = await db.execute(
        select(AuditEvent.user_email, func.max(AuditEvent.created_at).label("last_seen"))
        .where(AuditEvent.event_type == "guest_chat")
        .group_by(AuditEvent.user_email)
    )
    last_seen_map = {r[0]: r[1].strftime("%Y-%m-%d %H:%M") for r in last_seen_result.all()}

    # Per-guest token totals (all-time and this month)
    tokens_all_result = await db.execute(
        select(Message.guest_id, func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant")
        .group_by(Message.guest_id)
    )
    tokens_all_map = {r[0]: int(r[1]) for r in tokens_all_result.all()}

    tokens_month_result = await db.execute(
        select(Message.guest_id, func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant",
               Message.created_at >= month_start)
        .group_by(Message.guest_id)
    )
    tokens_month_map = {r[0]: int(r[1]) for r in tokens_month_result.all()}

    for g in top_guests:
        g["last_seen"] = last_seen_map.get(g["identifier"], "—")
        g["tokens_total"] = tokens_all_map.get(g["identifier"], 0)
        g["tokens_month"] = tokens_month_map.get(g["identifier"], 0)

    return {
        "total_guests": total_guests,
        "guests_today": guests_today,
        "total_events": total_events,
        "events_today": events_today,
        "total_tokens_all": int(total_tokens_all),
        "total_tokens_month": int(total_tokens_month),
        "events_by_day": events_by_day,
        "top_guests": top_guests,
    }


@router.get("/admin/users/{user_id}/stats")
async def admin_user_stats(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import cast, Date as SADate
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    total = await db.scalar(select(func.count(Message.id)).where(Message.user_id == user_id)) or 0
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = await db.scalar(
        select(func.count(Message.id)).where(Message.user_id == user_id, Message.created_at >= today_start)
    ) or 0
    sessions_count = await db.scalar(
        select(func.count(ChatSession.id)).where(ChatSession.user_id == user_id)
    ) or 0
    ps_blocked = await db.scalar(
        select(func.count(Message.id)).where(Message.user_id == user_id, Message.ps_action == "block")
    ) or 0

    days_result = await db.execute(
        select(cast(Message.created_at, SADate).label("day"), func.count(Message.id))
        .where(Message.user_id == user_id)
        .group_by("day").order_by("day").limit(14)
    )
    messages_by_day = [{"date": str(r[0]), "count": r[1]} for r in days_result.all()]

    model_result = await db.execute(
        select(Message.model, func.count(Message.id))
        .where(Message.user_id == user_id, Message.model.isnot(None))
        .group_by(Message.model).order_by(desc(func.count(Message.id))).limit(6)
    )
    messages_by_model = {r[0]: r[1] for r in model_result.all()}

    recent_result = await db.execute(
        select(Message).where(Message.user_id == user_id).order_by(desc(Message.id)).limit(20)
    )
    recent = [
        {
            "role": m.role,
            "content": m.content or "",
            "model": m.model,
            "ps_scanned": m.ps_scanned,
            "ps_action": m.ps_action,
            "prompt_tokens": m.prompt_tokens,
            "completion_tokens": m.completion_tokens,
            "total_tokens": m.total_tokens,
            "created_at": m.created_at.isoformat(),
        }
        for m in recent_result.scalars().all()
    ]

    return {
        "total_messages": total, "messages_today": today,
        "total_sessions": sessions_count, "ps_blocked": ps_blocked,
        "messages_by_day": messages_by_day, "messages_by_model": messages_by_model,
        "recent_messages": recent,
    }
