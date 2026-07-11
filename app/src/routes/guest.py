"""Open-mode guest routes: models, chat streaming, sanitize, and event logging."""
import json
import logging
import time
import uuid
from typing import Optional

import openai
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openai import AsyncOpenAI
from sqlalchemy.orm import selectinload

import database as _db_module
from auth import get_current_user
from crypto import decrypt
from database import get_db
from models import AuditEvent, ChatSession, Message, PSTenant, User
from prompt_security import PromptSecurityClient
from schemas import ChatMessage, PSTenantOut
from src.core.config import ALLOWED_SANITIZE_EXTENSIONS, ALLOWED_SANITIZE_TYPES, _SHARED_LLM_KEYS
from src.llm import catalog, routing
from src.services import ps
from src.services.audit import _log_audit
from src.services.file_extract import (
    _build_entity_contexts, _extract_file_text, _job_file_texts, _read_upload_with_limit,
)
from src.services.serializers import _build_content
from token_counter import estimate_message_tokens, estimate_text_tokens

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Guest endpoints (open mode — no auth) ────────────────────────────────────

class GuestPSConfig(BaseModel):
    base_url: Optional[str] = None
    gateway_url: Optional[str] = None
    app_id: Optional[str] = None
    mode: str = "api"
    enabled: bool = True

class GuestChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    skip_ps: bool = False
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    ps_config: Optional[GuestPSConfig] = None
    session_id: Optional[str] = None


@router.get("/guest/models")
async def guest_models():
    live = catalog._model_cache or await catalog.refresh_model_cache()
    available = live if live else catalog._FALLBACK_MODELS
    shared_providers = {k for k, v in _SHARED_LLM_KEYS.items() if v}
    enriched = []
    for m in available:
        meta = routing._model_meta(m["id"])
        provider = routing._detect_provider(m["id"])
        key_set = provider in shared_providers
        enriched.append({**m, **meta, "key_set": key_set})
    return {"models": enriched, "fallback": not bool(live)}


@router.get("/guest/ps-tenants", response_model=list[PSTenantOut])
async def guest_ps_tenants(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PSTenant).order_by(PSTenant.name))
    return result.scalars().all()


_GUEST_LOG_ALLOWED = {"ps_config_changed", "scenario_created", "scenario_updated", "scenario_deleted", "guest_registered"}
_USER_LOG_ALLOWED  = {"scenario_created", "scenario_updated", "scenario_deleted"}

@router.post("/users/log-event", status_code=204)
async def user_log_event(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event_type = (body.get("event_type") or "").strip()
    if event_type not in _USER_LOG_ALLOWED:
        return
    detail = (body.get("detail") or "")[:8000]
    await _log_audit(db, current_user.id, current_user.email, event_type, detail)


@router.post("/guest/log-event", status_code=204)
async def guest_log_event(body: dict, http_request: Request, db: AsyncSession = Depends(get_db)):
    event_type = (body.get("event_type") or "").strip()
    if event_type not in _GUEST_LOG_ALLOWED:
        return
    detail      = (body.get("detail")       or "")[:8000]
    guest_name  = (body.get("guest_name")   or "Guest")[:120]
    guest_email = (body.get("guest_email")  or "").strip().lower()[:255]
    ip          = http_request.client.host if http_request.client else "unknown"
    if guest_email and guest_name:
        user_email = f"{guest_email} ({guest_name})"
    elif guest_email:
        user_email = guest_email
    else:
        user_email = f"{guest_name} ({ip}) [open mode]"
    await _log_audit(db, None, user_email, event_type, detail)


@router.post("/guest/upload/sanitize")
async def guest_upload_sanitize(
    file: UploadFile = File(...),
    ps_base_url: str = Form(default=""),
    ps_app_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Open-mode file sanitization.
    PS config is taken from the client (localStorage) when present,
    otherwise falls back to the server-side admin PS config."""
    base_url = ps_base_url.strip()
    app_id   = ps_app_id.strip()

    if not base_url or not app_id:
        # Fall back to the admin user's PS tenant + key
        admin = await db.scalar(
            select(User).where(User.role == "admin", User.is_active == True)
            .options(selectinload(User.ps_tenant))
        )
        if admin and admin.ps_tenant and admin.ps_api_key_enc:
            base_url = admin.ps_tenant.base_url
            try:
                app_id = decrypt(admin.ps_api_key_enc)
            except Exception:
                pass

    if not base_url or not app_id:
        # PS not configured — return a pass result so the demo still works
        return {"job_id": "", "action": "pass", "violations": [], "sanitized_url": None, "scan_ms": 0}

    ps_client = ps.PromptSecurityClient(base_url=base_url, app_id=app_id)
    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    filename = file.filename or "upload"
    if mime not in ALLOWED_SANITIZE_TYPES and not filename.lower().endswith(ALLOWED_SANITIZE_EXTENSIONS):
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{mime}'.")
    file_bytes = await _read_upload_with_limit(file)
    file_text = _extract_file_text(file_bytes, filename)
    t0 = time.monotonic()
    try:
        result, request_info = await ps_client.sanitize_file(file_bytes, filename)
    except Exception as e:
        logger.error("Guest file sanitization error: %s", e)
        raise HTTPException(status_code=502, detail=f"PS file sanitization failed: {e}")
    scan_ms = round((time.monotonic() - t0) * 1000)
    action = result.get("action", result.get("status", "pass"))
    violations = result.get("violations", [])
    sanitized_url = result.get("sanitizedFileUrl") or result.get("sanitized_file_url") or result.get("url")
    findings = (result.get("metadata") or {}).get("findings") or result.get("findings") or {}
    job_id = result.get("jobId", "")
    # Store text in case this is async and we need it for the poll
    if job_id and file_text:
        _job_file_texts[job_id] = file_text
    entity_contexts = _build_entity_contexts(file_text, findings) if findings else {}
    return {
        "job_id": job_id,
        "action": action,
        "violations": violations,
        "sanitized_url": sanitized_url,
        "scan_ms": scan_ms,
        "raw": result,
        "request_info": request_info,
        "entity_contexts": entity_contexts,
    }


@router.post("/guest/chat/stream")
async def guest_chat_stream(
    request: GuestChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages list cannot be empty")

    available = catalog._model_cache or catalog._FALLBACK_MODELS
    model = request.model or (available[0]["id"] if available else None)
    if not model:
        raise HTTPException(status_code=503, detail="No models available")

    last_user_msg = next((m.content for m in reversed(request.messages) if m.role == "user"), None)
    if not last_user_msg:
        raise HTTPException(status_code=400, detail="No user message found")

    # Build guest identifier from email, name, and IP
    local_ip = http_request.client.host if http_request.client else "unknown"
    forwarded = http_request.headers.get("X-Forwarded-For", "")
    public_ip = forwarded.split(",")[0].strip() if forwarded else local_ip
    ip_label = f"{public_ip}" if public_ip == local_ip else f"{public_ip} / {local_ip}"
    name  = (request.guest_name  or "").strip()
    email = (request.guest_email or "").strip().lower()
    # Audit identifier: "email (name)" when both present, fallback to name or IP
    if email and name:
        guest_id = f"{email} ({name})"
    elif email:
        guest_id = email
    elif name:
        guest_id = name
    else:
        guest_id = f"guest:{ip_label}"
    # Username sent to Prompt Security: prefer email, fall back to name/ip
    ps_user = email or name or f"guest:{ip_label}"

    # Ensure a ChatSession exists for this guest
    session_id = request.session_id or str(uuid.uuid4())
    session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if not session:
        title = last_user_msg[:60] if last_user_msg else "Guest Chat"
        session = ChatSession(id=session_id, user_id=None, guest_id=guest_id, title=title)
        db.add(session)
        await db.flush()
    # Persist the user message (only the last one, to avoid re-saving history on each turn)
    db.add(Message(
        session_id=session_id, user_id=None, guest_id=guest_id,
        role="user", content=last_user_msg, model=model,
    ))
    await db.commit()

    system_prompt = request.system_prompt or "You are a helpful AI assistant."

    cfg = request.ps_config
    ps_client: Optional[PromptSecurityClient] = None
    ps_gw_client: Optional[AsyncOpenAI] = None
    ps_mode = cfg.mode if cfg else "api"

    if cfg and cfg.enabled and cfg.base_url and cfg.app_id and not request.skip_ps:
        if ps_mode == "api":
            ps_client = ps.PromptSecurityClient(base_url=cfg.base_url, app_id=cfg.app_id)
        elif ps_mode == "gateway" and cfg.gateway_url:
            gw_host = cfg.gateway_url.rstrip("/")
            gw_base = gw_host if gw_host.endswith("/v1") else gw_host + "/v1"
            provider = routing._detect_provider(model)
            llm_key = _SHARED_LLM_KEYS.get(provider, "")
            if llm_key:
                ps_gw_client = AsyncOpenAI(
                    api_key=llm_key,
                    base_url=gw_base,
                    timeout=30.0,
                    default_headers={"ps-app-id": cfg.app_id},
                )

    async def _persist_guest(content: str, action: str, scanned: bool, violations: list, elapsed_ms: int,
                             prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
                             total_tokens: Optional[int] = None):
        try:
            ps_tag = f" [PS:{action}]" if scanned else ""
            detail = f"{model}{ps_tag} [{ip_label}] — {last_user_msg[:80]}"
            async with _db_module.AsyncSessionLocal() as log_db:
                log_db.add(Message(
                    session_id=session_id, user_id=None, guest_id=guest_id,
                    role="assistant", content=content, model=model,
                    ps_scanned=scanned, ps_action=action,
                    ps_violations=violations,
                    response_ms=elapsed_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ))
                log_db.add(AuditEvent(
                    user_id=None, user_email=guest_id,
                    event_type="guest_chat", detail=detail[:500],
                ))
                await log_db.commit()
        except Exception as log_err:
            logger.warning("Guest activity log failed: %s", log_err)

    async def generate():
        reply = ""
        prompt_action = "pass"
        t0 = time.monotonic()
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        total_tokens: Optional[int] = None
        ps_prompt_raw: Optional[dict] = None
        ps_resp_raw: Optional[dict] = None

        payload = [{"role": "system", "content": system_prompt}] + [
            {"role": m.role, "content": _build_content(m)} for m in request.messages
        ]

        # Gateway mode
        if ps_gw_client:
            try:
                prompt_tokens = estimate_message_tokens(payload, model=model)
                stream = await ps_gw_client.chat.completions.create(
                    model=model, messages=payload, stream=True
                )
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                        completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                        total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        reply += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            except openai.BadRequestError as e:
                body_str = str(e).lower()
                elapsed = int((time.monotonic() - t0) * 1000)
                if any(w in body_str for w in ("block", "policy", "violat", "denied")):
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': []})}\n\n"
                    await _persist_guest("[BLOCKED by Prompt Security Gateway]", "block", True, [], elapsed)
                else:
                    yield f"data: {json.dumps({'type': 'error', 'detail': f'Gateway error: {e}'})}\n\n"
                return
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'detail': f'Gateway error: {e}'})}\n\n"
                return
            elapsed = int((time.monotonic() - t0) * 1000)
            completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
            prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
            total_tokens = total_tokens or (prompt_tokens + completion_tokens)
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': 0, 'daily_limit': None, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens})}\n\n"
            await _persist_guest(reply, "gateway", True, [], elapsed, prompt_tokens, completion_tokens, total_tokens)
            return

        # API mode: PS prompt scan
        if ps_client:
            try:
                ps_result = await ps_client.protect_prompt(
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=ps_user,
                )
                ps_prompt_raw = {"request": ps_result.raw_request, "response": ps_result.raw}
                if not ps_result.allowed:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': ps_result.violations, 'ps_raw': {'prompt': ps_prompt_raw}})}\n\n"
                    await _persist_guest("[BLOCKED by Prompt Security]", "block", True, ps_result.violations, elapsed)
                    return
                if ps_result.modified_text:
                    prompt_action = "modify"
                    for i in range(len(payload) - 1, -1, -1):
                        if payload[i]["role"] == "user":
                            payload[i]["content"] = ps_result.modified_text
                            break
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'detail': f'PS scan failed: {e}'})}\n\n"
                return

        # LLM stream — direct call to the detected provider using the shared key
        try:
            llm, effective_model = routing._guest_llm_client(model)
            prompt_tokens = estimate_message_tokens(payload, model=effective_model)
            stream = await llm.chat.completions.create(
                model=effective_model,
                messages=payload,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                    completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                    total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    reply += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
        except Exception as e:
            logger.error("Guest LLM stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"
            return

        # API mode: PS response scan
        ps_violations: list = []
        if ps_client and reply:
            try:
                ps_resp = await ps_client.protect_response(
                    response_text=reply,
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=ps_user,
                )
                ps_resp_raw = {"request": ps_resp.raw_request, "response": ps_resp.raw}
                ps_violations = ps_resp.violations
                if not ps_resp.allowed:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    yield f"data: {json.dumps({'type': 'revoke', 'action': 'block', 'violations': ps_violations, 'ps_raw': {'prompt': ps_prompt_raw, 'response': ps_resp_raw}})}\n\n"
                    await _persist_guest("[RESPONSE BLOCKED by Prompt Security]", "block", True, ps_violations, elapsed)
                    return
                if ps_resp.modified_text:
                    reply = ps_resp.modified_text
                    prompt_action = "modify"
                    yield f"data: {json.dumps({'type': 'sanitized', 'text': reply})}\n\n"
            except Exception as e:
                logger.warning("Guest PS response scan failed: %s", e)

        elapsed = int((time.monotonic() - t0) * 1000)
        completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
        prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
        total_tokens = total_tokens or (prompt_tokens + completion_tokens)
        ps_active = bool(ps_client)
        ps_raw_payload = {"prompt": ps_prompt_raw, "response": ps_resp_raw} if ps_active else None
        yield f"data: {json.dumps({'type': 'done', 'model': model, 'ps_scanned': ps_active, 'ps_action': prompt_action, 'ps_violations': ps_violations, 'messages_today': 0, 'daily_limit': None, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens, 'ps_raw': ps_raw_payload})}\n\n"
        await _persist_guest(reply, prompt_action, ps_active, ps_violations, elapsed, prompt_tokens, completion_tokens, total_tokens)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
