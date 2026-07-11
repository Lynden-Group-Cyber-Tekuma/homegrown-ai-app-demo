"""Application startup/shutdown: schema init, bootstrap admin, DB-backed settings."""
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select, text

from auth import hash_password, set_secret_key
from crypto import decrypt
from database import AsyncSessionLocal, Base, engine
from models import AppSetting, User
from src.core import config
from src.core.config import KNOWN_PROVIDERS, _SHARED_LLM_KEYS
from src.core.security import _validate_security_bootstrap_config
from src.llm import catalog
from src.services.scenarios_seed import _seed_demo_scenarios

logger = logging.getLogger(__name__)

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_security_bootstrap_config()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Legacy schema migrations for pre-existing Postgres databases
        # (Postgres dialect only — a fresh SQLite database is fully created
        # by create_all above and needs none of these).
        if engine.dialect.name == "postgresql":
            for sql in [
                "ALTER TABLE chat_sessions ALTER COLUMN user_id DROP NOT NULL",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS guest_id VARCHAR(255)",
                "CREATE INDEX IF NOT EXISTS ix_chat_sessions_guest_id ON chat_sessions (guest_id)",
                "ALTER TABLE messages ALTER COLUMN user_id DROP NOT NULL",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS guest_id VARCHAR(255)",
                "CREATE INDEX IF NOT EXISTS ix_messages_guest_id ON messages (guest_id)",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS completion_tokens INTEGER",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS total_tokens INTEGER",
                "ALTER TABLE audit_events ALTER COLUMN detail TYPE TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
            ]:
                try:
                    await conn.execute(text(sql))
                except Exception as e:
                    logger.debug("Migration skipped (%s): %s", sql[:60], e)

    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(User).where(User.role == "admin"))
        if not existing:
            import secrets as _secrets
            admin = User(
                email=f"setup-{_secrets.token_hex(8)}@wizard.internal",
                hashed_password=hash_password(_secrets.token_hex(32)),
                role="admin",
                is_active=True,
                must_change_password=True,
            )
            db.add(admin)
            await db.commit()
            logger.info("Bootstrap admin placeholder created (id=%s)", admin.id)

    async with AsyncSessionLocal() as db:
        await _seed_demo_scenarios(db)

    # Override JWT secret from DB if saved via the admin UI
    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, "jwt_secret_enc")
        if row:
            try:
                set_secret_key(decrypt(row.value))
                logger.info("JWT secret loaded from app_settings")
            except Exception as exc:
                logger.warning("Could not load JWT secret from DB: %s", exc)
        # Load stored provider API keys into _SHARED_LLM_KEYS (override env var values)
        for p in KNOWN_PROVIDERS:
            pk_row = await db.get(AppSetting, f"provider_key_{p['id']}")
            if pk_row:
                try:
                    _SHARED_LLM_KEYS[p["id"]] = decrypt(pk_row.value)
                except Exception:
                    pass

        # Load hot-swappable application settings
        dl_row = await db.get(AppSetting, "daily_limit")
        if dl_row:
            config.DEFAULT_DAILY_LIMIT = int(dl_row.value) or None
        mf_row = await db.get(AppSetting, "max_file_mb")
        if mf_row:
            config.MAX_FILE_SIZE_MB = int(mf_row.value)
            config.MAX_FILE_SIZE_BYTES = config.MAX_FILE_SIZE_MB * 1024 * 1024

        # Load previously discovered provider models
        dm_row = await db.get(AppSetting, "discovered_models")
        if dm_row:
            try:
                catalog._DISCOVERED_MODELS.update(json.loads(dm_row.value))
                logger.info("Loaded discovered models for providers: %s", list(catalog._DISCOVERED_MODELS.keys()))
            except Exception as exc:
                logger.warning("Could not load discovered models: %s", exc)

    await catalog.refresh_model_cache()
    yield

