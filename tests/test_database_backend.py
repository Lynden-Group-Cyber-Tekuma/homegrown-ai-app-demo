"""Tests for database backend selection (SQLite default, Postgres via env)."""

import os
from pathlib import Path

import pytest

from database import _POSTGRES_DEFAULT_URL, _resolve_database_url


@pytest.fixture
def _clean_db_env(monkeypatch):
    """Strip all database-related env vars so each test sets exactly what it needs."""
    for var in ("DATABASE_URL", "DB_BACKEND", "SQLITE_PATH"):
        monkeypatch.delenv(var, raising=False)


def test_default_is_sqlite(_clean_db_env, monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "data" / "hgapp.db"))

    url = _resolve_database_url()

    assert url == f"sqlite+aiosqlite:///{tmp_path / 'data' / 'hgapp.db'}"
    # the parent directory is created so SQLite can open the file
    assert (tmp_path / "data").is_dir()


def test_db_backend_postgres_uses_local_server(_clean_db_env, monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "postgres")

    assert _resolve_database_url() == _POSTGRES_DEFAULT_URL


def test_db_backend_is_case_insensitive(_clean_db_env, monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "  PostgreSQL ")

    assert _resolve_database_url() == _POSTGRES_DEFAULT_URL


def test_database_url_env_wins_over_db_backend(_clean_db_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
    monkeypatch.setenv("DB_BACKEND", "sqlite")

    assert _resolve_database_url() == "postgresql+asyncpg://u:p@host:5432/db"


def test_override_file_url_wins_over_everything(_clean_db_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
    monkeypatch.setenv("DB_BACKEND", "postgres")

    assert _resolve_database_url("sqlite+aiosqlite:///override.db") == "sqlite+aiosqlite:///override.db"


def test_unknown_db_backend_falls_back_to_sqlite(_clean_db_env, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_BACKEND", "mysql")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "x.db"))

    assert _resolve_database_url().startswith("sqlite+aiosqlite:///")


@pytest.mark.asyncio
async def test_app_boots_against_file_sqlite(tmp_path):
    """End-to-end: schema creation and a session round-trip on a file-based SQLite DB."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from database import Base
    from models import AppSetting

    db_file = tmp_path / "hgapp.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(AppSetting(key="smoke", value="ok"))
        await session.commit()
        row = await session.scalar(select(AppSetting).where(AppSetting.key == "smoke"))
        assert row is not None and row.value == "ok"

    await engine.dispose()
    assert db_file.exists()
