import json
import logging
import os
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

# Override file: persists a new DATABASE_URL across restarts.
# Lives in app/data/ next to the SQLite database file.
_DB_OVERRIDE_FILE = Path(os.getenv(
    "DB_OVERRIDE_FILE",
    str(Path(__file__).parent / "data" / "db_config_override.json"),
))

_POSTGRES_DEFAULT_URL = "postgresql+asyncpg://hgapp:hgapp_dev@localhost:5432/hgapp"


def _load_override_url() -> str | None:
    try:
        if _DB_OVERRIDE_FILE.exists():
            data = json.loads(_DB_OVERRIDE_FILE.read_text())
            url = data.get("database_url")
            if url:
                logger.info("Using DATABASE_URL from override file: %s", _DB_OVERRIDE_FILE)
                return url
    except Exception as exc:
        logger.warning("Failed to read DB override file: %s", exc)
    return None


def _default_sqlite_path() -> Path:
    """SQLite file location: SQLITE_PATH env var, else app/data/hgapp.db."""
    return Path(os.getenv("SQLITE_PATH", str(Path(__file__).parent / "data" / "hgapp.db")))


def _resolve_database_url(override_url: str | None = None) -> str:
    """Pick the database URL by precedence:

    1. override file (admin-persisted)
    2. DATABASE_URL env var (any SQLAlchemy async URL — SQLite or Postgres)
    3. DB_BACKEND=postgres env var → local Postgres server
    4. default: file-based SQLite (path from SQLITE_PATH, else app/data/hgapp.db)
    """
    if override_url:
        return override_url
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    if os.getenv("DB_BACKEND", "").strip().lower() in {"postgres", "postgresql"}:
        return _POSTGRES_DEFAULT_URL
    sqlite_path = _default_sqlite_path()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{sqlite_path}"


DATABASE_URL: str = _resolve_database_url(_load_override_url())

_engine_kwargs: dict = {"echo": False}
if not DATABASE_URL.startswith("sqlite"):
    # Connection health checks only make sense for networked databases.
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

if engine.dialect.name == "sqlite":
    logger.info("Using SQLite database: %s", DATABASE_URL)

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _record):
        # Match Postgres behavior (FK enforcement) and improve concurrent
        # read/write behavior for file-based databases (WAL is a no-op for
        # in-memory databases).
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
