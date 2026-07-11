"""File sanitization routes (Prompt Security sanitizeFile API)."""
import logging
import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import get_current_user
from crypto import decrypt
from database import get_db
from models import User
from src.core import config
from src.core.config import ALLOWED_SANITIZE_EXTENSIONS, ALLOWED_SANITIZE_TYPES
from src.services import ps
from src.services.file_extract import (
    _build_entity_contexts, _extract_file_text, _job_file_texts, _read_upload_with_limit,
)
from src.services.sanitize_guard import (
    _acquire_sanitize_slot, _release_sanitize_slot, _sanitize_guard_lock, _sanitize_user_timestamps,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── File Sanitization ─────────────────────────────────────────────────────────
@router.post("/upload/sanitize")
async def upload_sanitize(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if not (current_user.ps_tenant and current_user.ps_api_key_enc):
        raise HTTPException(status_code=400, detail="Prompt Security is not configured. Set your PS region and App ID in Settings.")
    try:
        ps_app_id = decrypt(current_user.ps_api_key_enc)
    except ValueError:
        raise HTTPException(status_code=400, detail="PS App ID could not be decrypted — re-enter it in Settings.")
    ps_client = ps.PromptSecurityClient(base_url=current_user.ps_tenant.base_url, app_id=ps_app_id)
    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    filename = file.filename or "upload"
    if mime not in ALLOWED_SANITIZE_TYPES and not filename.lower().endswith(ALLOWED_SANITIZE_EXTENSIONS):
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{mime}'.")

    await _acquire_sanitize_slot(current_user.id)
    try:
        file_bytes = await _read_upload_with_limit(file)

        # Record per-minute quota only after local validation/read succeeds.
        now = time.time()
        async with _sanitize_guard_lock:
            timestamps = _sanitize_user_timestamps[current_user.id]
            while timestamps and now - timestamps[0] > 60:
                timestamps.popleft()
            if len(timestamps) >= config.SANITIZE_MAX_PER_MINUTE:
                raise HTTPException(status_code=429, detail="Sanitize rate limit exceeded")
            timestamps.append(now)

        file_text = _extract_file_text(file_bytes, file.filename or "")
        t0 = time.monotonic()
        try:
            result, request_info = await ps_client.sanitize_file(file_bytes, file.filename or "upload")
        except Exception as e:
            logger.error("File sanitization error for %s: %s", current_user.email, e)
            raise HTTPException(status_code=502, detail=f"PS file sanitization failed: {e}")
        scan_ms = round((time.monotonic() - t0) * 1000)
        job_id = result.get("jobId", "")
        # PS sanitizeFile wraps action/violations inside result["metadata"]
        _meta = result.get("metadata") or {}
        action = _meta.get("action") or result.get("action") or result.get("status", "pass")
        violations = _meta.get("violations") or result.get("violations") or []
        sanitized_url = result.get("sanitizedFileUrl") or result.get("sanitized_file_url") or result.get("url")
        findings = (_meta.get("findings") or result.get("findings") or {})
        # Store text for context building once the async job completes
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
    finally:
        await _release_sanitize_slot(current_user.id)


@router.get("/upload/sanitize/status")
async def upload_sanitize_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    """Poll PS for the result of a previously submitted sanitize job."""
    if not (current_user.ps_tenant and current_user.ps_api_key_enc):
        raise HTTPException(status_code=400, detail="Prompt Security is not configured.")
    ps_app_id = decrypt(current_user.ps_api_key_enc)
    url = f"{current_user.ps_tenant.base_url}/api/sanitizeFile"
    request_info = {
        "method": "GET",
        "url": f"{url}?jobId={job_id}",
        "headers": {"APP-ID": f"{ps_app_id[:6]}…"},
    }
    try:
        async with __import__("httpx").AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"jobId": job_id}, headers={"APP-ID": ps_app_id})
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PS status check failed: {e}")
    _meta = result.get("metadata") or {}
    action = _meta.get("action") or result.get("action") or result.get("status", "processing")
    violations = _meta.get("violations") or result.get("violations") or []
    sanitized_url = result.get("sanitizedFileUrl") or result.get("sanitized_file_url") or result.get("url")
    findings = (_meta.get("findings") or result.get("findings") or {})
    result.setdefault("jobId", job_id)
    # Build context snippets now that we have findings; consume stored text
    file_text = _job_file_texts.pop(job_id, "") if findings else _job_file_texts.get(job_id, "")
    entity_contexts = _build_entity_contexts(file_text, findings) if findings else {}
    return {
        "job_id": job_id,
        "action": action,
        "violations": violations,
        "sanitized_url": sanitized_url,
        "raw": result,
        "request_info": request_info,
        "entity_contexts": entity_contexts,
    }


@router.get("/guest/upload/sanitize/status")
async def guest_upload_sanitize_status(
    job_id: str,
    ps_base_url: str = "",
    ps_app_id: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Poll PS for the result of a guest sanitize job."""
    base_url = ps_base_url.strip()
    app_id   = ps_app_id.strip()
    if not base_url or not app_id:
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
        raise HTTPException(status_code=400, detail="Prompt Security is not configured.")
    url = f"{base_url}/api/sanitizeFile"
    request_info = {
        "method": "GET",
        "url": f"{url}?jobId={job_id}",
        "headers": {"APP-ID": f"{app_id[:6]}…"},
    }
    try:
        async with __import__("httpx").AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"jobId": job_id}, headers={"APP-ID": app_id})
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PS status check failed: {e}")
    _meta = result.get("metadata") or {}
    action = _meta.get("action") or result.get("action") or result.get("status", "processing")
    violations = _meta.get("violations") or result.get("violations") or []
    sanitized_url = result.get("sanitizedFileUrl") or result.get("sanitized_file_url") or result.get("url")
    findings = (_meta.get("findings") or result.get("findings") or {})
    result.setdefault("jobId", job_id)
    file_text = _job_file_texts.pop(job_id, "") if findings else _job_file_texts.get(job_id, "")
    entity_contexts = _build_entity_contexts(file_text, findings) if findings else {}
    return {
        "job_id": job_id,
        "action": action,
        "violations": violations,
        "sanitized_url": sanitized_url,
        "raw": result,
        "request_info": request_info,
        "entity_contexts": entity_contexts,
    }
