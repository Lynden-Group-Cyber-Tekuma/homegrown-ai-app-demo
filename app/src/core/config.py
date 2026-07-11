"""Environment-driven configuration constants and shared key state.

Values that can change at runtime (admin settings PATCH, startup DB overrides)
are read and written as module attributes: `config.MAX_FILE_SIZE_MB`, etc.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
ADMIN_EMAIL        = os.getenv("ADMIN_EMAIL", "admin@sentinelone.com")
ADMIN_PASSWORD     = os.getenv("ADMIN_PASSWORD", "ChangeMe!")
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "50")) or None
SHOW_LLM_KEY_SETTINGS = os.getenv("SHOW_LLM_KEY_SETTINGS", "false").lower() in {"1", "true", "yes", "on"}
APP_ENV = os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()

# ── Email / SMTP (env-var fallbacks; DB values take precedence) ──────────────
SMTP_HOST      = os.getenv("SMTP_HOST", "")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", ADMIN_EMAIL)
APP_BASE_URL   = os.getenv("APP_BASE_URL", "").rstrip("/")


# Shared LLM keys (fallback when user has no per-provider key; DB values loaded at startup)
_SHARED_LLM_KEYS = {
    "openai":      os.getenv("OPENAI_API_KEY", ""),
    "anthropic":   os.getenv("ANTHROPIC_API_KEY", ""),
    "google":      os.getenv("GOOGLE_API_KEY", ""),
    "perplexity":  os.getenv("PERPLEXITY_API_KEY", ""),
    "openrouter":  os.getenv("OPENROUTER_API_KEY", ""),
}

KNOWN_PROVIDERS = [
    {"id": "openai",     "name": "OpenAI",     "env": "OPENAI_API_KEY"},
    {"id": "anthropic",  "name": "Anthropic",  "env": "ANTHROPIC_API_KEY"},
    {"id": "google",     "name": "Google",     "env": "GOOGLE_API_KEY"},
    {"id": "perplexity", "name": "Perplexity", "env": "PERPLEXITY_API_KEY"},
    {"id": "openrouter", "name": "OpenRouter", "env": "OPENROUTER_API_KEY"},
]


# ── File upload limits ────────────────────────────────────────────────────────
# Set MAX_FILE_SIZE_MB in .env to restrict upload size.
MAX_FILE_SIZE_MB    = int(os.getenv("MAX_FILE_SIZE_MB") or "10")
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_TEXT_TYPES  = {"text/plain", "text/markdown", "text/csv", "application/json"}
ALLOWED_PDF_TYPE    = "application/pdf"
ALLOWED_SANITIZE_TYPES = {
    # Documents
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroEnabled.12",
    "application/rtf", "text/rtf",
    "application/vnd.oasis.opendocument.text",
    # Spreadsheets
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
    "text/csv", "text/tab-separated-values",
    # Presentations
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.template",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    # Images
    "image/bmp", "image/heic", "image/jpeg", "image/png", "image/tiff",
    # Email
    "message/rfc822", "application/vnd.ms-outlook",
    # Text/markup
    "text/plain", "text/html", "text/xml", "application/xml",
    "text/markdown", "text/x-rst", "text/x-org",
    # Other
    "application/epub+zip",
    "application/zip",
}
ALLOWED_SANITIZE_EXTENSIONS = (
    # Documents
    ".pdf", ".doc", ".docx", ".docm", ".dot", ".dotm", ".rtf", ".odt",
    ".abw", ".zabw", ".hwp",
    # Spreadsheets
    ".xls", ".xlsx", ".csv", ".tsv", ".dbf", ".dif", ".et", ".fods", ".mw",
    # Presentations
    ".ppt", ".pptx", ".pot", ".pptm",
    # Images
    ".bmp", ".heic", ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    # Email
    ".eml", ".msg", ".p7s",
    # Apple
    ".cwk", ".mcw",
    # Text / markup / other
    ".txt", ".html", ".htm", ".epub", ".md", ".org", ".rst", ".xml",
    ".zip", ".eth", ".pbd", ".prn", ".sdp", ".sxg",
)
SANITIZE_MAX_PER_MINUTE = int(os.getenv("SANITIZE_MAX_PER_MINUTE") or "5")
SANITIZE_MAX_CONCURRENT_PER_USER = int(os.getenv("SANITIZE_MAX_CONCURRENT_PER_USER") or "1")
