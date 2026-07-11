"""SMTP configuration resolution and verification-code email delivery."""
import email.mime.multipart
import email.mime.text
import logging
import smtplib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from crypto import decrypt
from models import AppSetting
from src.core.config import EMAIL_PASSWORD, EMAIL_USERNAME, FROM_EMAIL, SMTP_HOST, SMTP_PORT

logger = logging.getLogger(__name__)

# In-memory store: email -> {code, expires_at}  (cleared after verify or expiry)
_email_verify_codes: dict[str, dict] = {}
_EMAIL_CODE_TTL = 600  # 10 minutes


async def _get_email_config(db: AsyncSession) -> dict:
    """Return SMTP config merging DB values (preferred) with env-var fallbacks."""
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}

    # Password: decrypt from DB if present, else fall back to env var plaintext
    enc_pwd = s.get("email_password_enc")
    if enc_pwd:
        try:
            password = decrypt(enc_pwd)
        except Exception:
            password = EMAIL_PASSWORD
    else:
        password = EMAIL_PASSWORD

    try:
        port = int(s.get("smtp_port") or SMTP_PORT)
    except (ValueError, TypeError):
        port = SMTP_PORT

    return {
        "smtp_host":      s.get("smtp_host") or SMTP_HOST,
        "smtp_port":      port,
        "email_username": s.get("email_username") or EMAIL_USERNAME,
        "email_password": password,
        "from_email":     s.get("from_email") or FROM_EMAIL,
    }



def _send_verification_email(to_email: str, code: str, guest_name: str, config: dict) -> None:
    """Blocking SMTP send — run in a thread executor."""
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = "Your HGA Prompt Demo verification code"
    msg["From"]    = f"HGA Prompt Demo <{config['from_email']}>"
    msg["To"]      = to_email

    plain = (
        f"Hi {guest_name or 'there'},\n\n"
        f"Your verification code is: {code}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"— HGA Prompt Demo"
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'Inter',Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:40px 0">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0"
             style="background:#1a1d27;border-radius:14px;border:1px solid #2a2d3e;overflow:hidden">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#6c63ff 0%,#5a52e0 100%);padding:28px 36px;text-align:center">
            <img src="https://assets-global.website-files.com/656f4138f2ff78452cf12053/658da588005946f4a6cbd84e_webpac.png" width="32" height="32"
                 style="border-radius:6px;margin-bottom:10px;display:block;margin-left:auto;margin-right:auto"
                 alt="HGA">
            <div style="color:#fff;font-size:20px;font-weight:700;letter-spacing:-0.3px">HGA Prompt Demo</div>
            <div style="color:rgba(255,255,255,0.7);font-size:13px;margin-top:4px">Email Verification</div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:36px 36px 28px">
            <p style="color:#e8eaf6;font-size:15px;margin:0 0 8px">
              Hi <strong>{guest_name or 'there'}</strong>,
            </p>
            <p style="color:#7b80a8;font-size:13px;line-height:1.6;margin:0 0 28px">
              Use the code below to verify your email address and complete setup.
              This code expires in <strong style="color:#e8eaf6">10 minutes</strong>.
            </p>

            <!-- Code box -->
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr><td align="center" style="padding:4px 0 28px">
                <div style="display:inline-block;background:#20253a;border:2px solid #6c63ff;
                            border-radius:12px;padding:20px 36px;letter-spacing:14px;
                            font-size:36px;font-weight:800;color:#fff;font-family:monospace">
                  {code}
                </div>
              </td></tr>
            </table>

            <p style="color:#7b80a8;font-size:12px;line-height:1.6;margin:0">
              If you didn't request this code, you can safely ignore this email.
              Someone may have entered your address by mistake.
            </p>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:16px 36px 24px;border-top:1px solid #2a2d3e;text-align:center">
            <p style="color:#4a4f6a;font-size:11px;margin:0">
              Sent by HGA Prompt Demo &nbsp;·&nbsp; Powered by Prompt Security
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    msg.attach(email.mime.text.MIMEText(plain, "plain"))
    msg.attach(email.mime.text.MIMEText(html, "html"))

    smtp = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=10)
    try:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(config["email_username"], config["email_password"])
        smtp.sendmail(config["from_email"], to_email, msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass  # server may close connection before QUIT — email was already sent
