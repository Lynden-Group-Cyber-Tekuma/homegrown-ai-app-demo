"""Security validation helpers: bootstrap secret checks and external URL validation."""
import os
from typing import Optional
from urllib.parse import urlparse, urlunparse

from fastapi import HTTPException

from src.core import config

def _validate_security_bootstrap_config() -> None:
    """Raise RuntimeError in production if insecure default secrets are detected."""
    if config.APP_ENV in {"dev", "development", "test", "local"}:
        return

    _insecure_secret_keys = {"dev_secret_change_me", "change_me", "changeme", "test-secret-key-for-unit-tests"}
    _insecure_admin_passwords = {"admin", "change_me", "changeme", "password", "ChangeMe!"}

    secret_key = os.getenv("SECRET_KEY", "")
    admin_password = os.getenv("ADMIN_PASSWORD", "")

    if not secret_key or secret_key in _insecure_secret_keys:
        raise RuntimeError(
            "SECRET_KEY is not set or uses an insecure default. "
            "Set a strong SECRET_KEY environment variable before running in production."
        )
    if not admin_password or admin_password in _insecure_admin_passwords:
        raise RuntimeError(
            "ADMIN_PASSWORD is not set or uses an insecure default. "
            "Set a strong ADMIN_PASSWORD environment variable before running in production."
        )



_PRIVATE_IP_PREFIXES = (
    "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.",
    "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
    "172.29.", "172.30.", "172.31.", "192.168.", "127.", "169.254.",
)
_UNSAFE_HOSTNAMES = {"localhost", ""}


def _is_unsafe_host(hostname: str) -> bool:
    if hostname in _UNSAFE_HOSTNAMES:
        return True
    if hostname.endswith(".local"):
        return True
    return any(hostname.startswith(pfx) for pfx in _PRIVATE_IP_PREFIXES)


def _validate_external_https_url(url: str, field_name: str) -> str:
    """Validate that a URL is HTTPS and targets a public hostname. Raises HTTPException if not."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=422, detail=f"{field_name}: URL must use HTTPS")
    if _is_unsafe_host(parsed.hostname or ""):
        raise HTTPException(status_code=422, detail=f"{field_name}: URL must target a public hostname")
    return url


def _normalize_legacy_public_http_url(url: str) -> Optional[str]:
    """Upgrade http:// to https:// only for public hostnames. Returns None for private/localhost."""
    parsed = urlparse(url)
    if parsed.scheme != "http":
        return url
    if _is_unsafe_host(parsed.hostname or ""):
        return None
    return urlunparse(parsed._replace(scheme="https"))
