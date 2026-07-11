"""Prompt Security client construction and legacy tenant URL migration."""
import logging
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from crypto import decrypt
from models import User
from prompt_security import PromptSecurityClient
from src.core.security import _is_unsafe_host

logger = logging.getLogger(__name__)

async def _migrate_legacy_ps_tenant_urls(db: AsyncSession) -> None:
    """Disable ps_enabled for users whose PS tenant has an invalid (non-HTTPS/private) base_url."""
    result = await db.execute(
        select(User).where(User.ps_enabled.is_(True)).options(selectinload(User.ps_tenant))
    )
    users = result.scalars().all()
    for user in users:
        if user.ps_tenant and _is_unsafe_host(urlparse(user.ps_tenant.base_url).hostname or ""):
            user.ps_enabled = False
            logger.warning("Disabled PS for user %s — invalid tenant base_url: %s", user.email, user.ps_tenant.base_url)
    await db.commit()


def _build_ps_api_client(user: User) -> Optional[PromptSecurityClient]:
    if not (user.ps_enabled and user.ps_tenant and user.ps_api_key_enc):
        return None
    parsed = urlparse(user.ps_tenant.base_url)
    if parsed.scheme != "https" or _is_unsafe_host(parsed.hostname or ""):
        logger.warning("PS tenant base_url is invalid for %s: %s", user.email, user.ps_tenant.base_url)
        return None
    try:
        ps_app_id = decrypt(user.ps_api_key_enc)
    except ValueError:
        logger.warning("PS key decrypt failed for %s", user.email)
        return None
    if not ps_app_id:
        return None
    return PromptSecurityClient(base_url=user.ps_tenant.base_url, app_id=ps_app_id)
