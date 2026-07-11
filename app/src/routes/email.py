"""Email routes: SMTP tests and guest email verification codes."""
import asyncio
import logging
import random
import smtplib
import time
from typing import Optional

import email.mime.multipart
import email.mime.text

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin
from database import get_db
from models import User
from src.services.audit import _log_audit
from src.services.email_service import (
    _EMAIL_CODE_TTL, _email_verify_codes, _get_email_config, _send_verification_email,
)

logger = logging.getLogger(__name__)

router = APIRouter()

class TestEmailRequest(BaseModel):
    to: str


@router.post("/admin/test-smtp")
async def test_smtp_connection(
    body: dict,
    admin: User = Depends(require_admin),
):
    """Test SMTP connection with inline credentials — does not save settings."""
    host = (body.get("smtp_host") or "").strip()
    port = int(body.get("smtp_port") or 587)
    username = (body.get("email_username") or "").strip()
    password = (body.get("email_password") or "").strip()

    if not host:
        raise HTTPException(status_code=422, detail="SMTP host is required")

    def _test():
        smtp = smtplib.SMTP(host, port, timeout=10)
        try:
            smtp.ehlo()
            smtp.starttls()
            if username and password:
                smtp.login(username, password)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _test)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@router.post("/admin/test-email")
async def send_test_email(
    body: TestEmailRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send a test email to verify SMTP configuration."""
    cfg = await _get_email_config(db)
    if not cfg["smtp_host"]:
        raise HTTPException(status_code=503, detail="SMTP host is not configured")

    to = body.to.strip()
    if not to or "@" not in to:
        raise HTTPException(status_code=422, detail="Invalid recipient email address")

    def _send_test() -> None:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["Subject"] = "HGA Prompt Demo — Test Email"
        msg["From"]    = cfg["from_email"]
        msg["To"]      = to

        plain = (
            "This is a test email from HGA Prompt Demo.\n"
            "Your email settings are configured correctly."
        )
        html = (
            "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif'>"
            "<p>This is a test email from <strong>HGA Prompt Demo</strong>.</p>"
            "<p>Your email settings are configured correctly.</p>"
            "</body></html>"
        )
        msg.attach(email.mime.text.MIMEText(plain, "plain"))
        msg.attach(email.mime.text.MIMEText(html, "html"))

        smtp = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10)
        try:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["email_username"], cfg["email_password"])
            smtp.sendmail(cfg["from_email"], to, msg.as_string())
        finally:
            try:
                smtp.quit()
            except Exception:
                pass  # server may close connection before QUIT — email was already sent

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_test)
    except Exception as exc:
        logger.error("Test email send failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    await _log_audit(db, admin.id, admin.email, "email_settings_tested", f"Test email sent to {to}")

    return {"sent": True}



class EmailCodeRequest(BaseModel):
    email: str
    name: Optional[str] = ""


class EmailCodeVerify(BaseModel):
    email: str
    code: str


@router.post("/guest/request-email-code")
async def request_email_code(body: EmailCodeRequest, db: AsyncSession = Depends(get_db)):
    """Send a 4-digit verification code to the given email address."""
    cfg = await _get_email_config(db)
    if not cfg["smtp_host"]:
        raise HTTPException(status_code=503, detail="Email service not configured")
    to = body.email.strip().lower()
    if not to or "@" not in to:
        raise HTTPException(status_code=422, detail="Invalid email address")

    code = f"{random.randint(0, 9999):04d}"
    _email_verify_codes[to] = {"code": code, "expires_at": time.time() + _EMAIL_CODE_TTL}

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_verification_email, to, code, body.name or "", cfg)
    except Exception as exc:
        logger.error("Failed to send verification email to %s: %s", to, exc)
        raise HTTPException(status_code=502, detail="Failed to send email. Please check your address and try again.")

    return {"sent": True}


@router.post("/guest/verify-email-code")
async def verify_email_code(body: EmailCodeVerify):
    """Verify the 4-digit code sent to an email address."""
    to = body.email.strip().lower()
    entry = _email_verify_codes.get(to)
    if not entry:
        return {"valid": False, "reason": "no_code"}
    if time.time() > entry["expires_at"]:
        _email_verify_codes.pop(to, None)
        return {"valid": False, "reason": "expired"}
    if entry["code"] != body.code.strip():
        return {"valid": False, "reason": "wrong_code"}
    _email_verify_codes.pop(to, None)
    return {"valid": True}
