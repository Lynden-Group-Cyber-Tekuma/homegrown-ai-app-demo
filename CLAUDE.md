# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Changelog

This project maintains a `CHANGELOG.md` at the repo root in [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format (date-based, no semver).

**Rule:** Whenever you implement a feature, fix, or any meaningful change, add an entry to `CHANGELOG.md` under today's date before committing. Use subsections `### Added`, `### Changed`, `### Fixed`, `### Security` as appropriate. Put the newest date at the top.

**Rule:** Each changelog entry must include the author who owns the change. Use only the username portion of their email (the part before `@`) as an incognito identifier, e.g. `— @johndoe`. If Claude implements the change autonomously, attribute it to the user who requested it.

---

## Commands

### Run the app (SQLite default — no Docker, no database server)
```bash
pip install -r requirements.txt
cd app && uvicorn main:app --reload --port 8000   # SQLite file created at app/data/hgapp.db
```

### Run against PostgreSQL instead
```bash
# local server matching the defaults (hgapp:hgapp_dev@localhost:5432/hgapp):
cd app && DB_BACKEND=postgres uvicorn main:app --reload --port 8000
# or any server via a full URL:
cd app && DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/dbname" uvicorn main:app --port 8000
```

### Run tests
```bash
pip install -r requirements-test.txt
pytest                         # all tests (uses SQLite in-memory)
pytest tests/test_app_endpoints.py          # single file
pytest tests/test_chat_stream.py::test_name # single test
```

Tests use SQLite in-memory via `conftest.py` — no running Postgres needed.

---

## Architecture

**Modular FastAPI backend.** `app/main.py` is a ~25-line entrypoint that builds the FastAPI app (`uvicorn main:app` from `app/`, unchanged) and includes routers from the `app/src/` package tree:

- `src/core/` — `config.py` (env-driven constants + runtime-mutable settings, always accessed as `config.X`), `security.py` (bootstrap secret checks, URL validation), `lifespan.py` (startup: schema init, bootstrap admin, DB-backed settings)
- `src/llm/` — `routing.py` (provider detection + direct OpenAI-compatible clients), `catalog.py` (discovered/fallback model lists + cache), `discovery.py` (provider `/models` queries + persistence)
- `src/services/` — `audit.py` (`_log_audit`/`_log_msg`), `serializers.py`, `ps.py` (PS client construction; monkeypatch `ps.PromptSecurityClient` in tests), `email_service.py`, `file_extract.py`, `sanitize_guard.py` (rate/concurrency stores), `scenarios_seed.py`
- `src/routes/` — one module per route group (`auth`, `system`, `users_me`, `admin_users`, `admin_tenants`, `admin_stats`, `sessions`, `uploads`, `chat`, `sanitize`, `activity`, `app_settings`, `provider_keys`, `email`, `guest`, `scenarios`, `html`), each exposing an `APIRouter` collected by `src/routes/__init__.py`

Rule: runtime-mutable settings (`MAX_FILE_SIZE_MB`, `DEFAULT_DAILY_LIMIT`, `SANITIZE_MAX_*`, `APP_ENV`) live in `src/core/config.py` and must be read/written as `config.X` attributes — never `from ... import` them — so admin-settings PATCHes and test monkeypatching stay effective.

Top-level support modules (imported flat because `app/` is the working directory):

- `models.py` — SQLAlchemy ORM (async): `PSTenant`, `User`, `ChatSession`, `Message`, `APIKey`, `AuditEvent`
- `schemas.py` — Pydantic v2 request/response types
- `auth.py` — JWT issuance/validation, API key hashing, `require_admin` dependency
- `crypto.py` — Fernet encryption for LLM API keys and PS App IDs stored in DB
- `database.py` — async SQLAlchemy engine + `get_db` session dependency
- `prompt_security.py` — `PromptSecurityClient`: wraps `POST /api/protect` and `POST /api/sanitizeFile`
- `token_counter.py` — token estimation via the `litellm` Python library's tokenizer (library only — there is no LiteLLM proxy service)
- `app/static/` — three self-contained HTML files (no build step, no npm): `index.html` (chat UI), `admin.html` (dashboard), `login.html`

**Direct provider routing** — the only LLM path. When a shared API key is saved for OpenAI, Anthropic, Google, Perplexity, or OpenRouter in the admin Settings panel, the app queries that provider's `/models` endpoint and adds all available models to the picker as `provider/model-id` IDs (e.g. `openai/gpt-4.1`). Chat calls go directly to the provider's OpenAI-compatible endpoint via `_user_llm_client()` / `_guest_llm_client()` (per-user key → shared key; `LookupError` if neither is set). Discovered models are persisted in the `AppSetting` table.

### Key data flows

**Chat (streaming):** `POST /chat/stream` → PS prompt scan (API mode) or pass-through (gateway mode) → direct provider LLM call → PS response scan → SSE to browser. Gateway mode routes through the PS proxy URL instead of calling PS explicitly.

**File scan:** `POST /upload/sanitize` or `POST /guest/upload/sanitize` → PS two-step async API: `POST /api/sanitizeFile` (returns `jobId`) → `GET /api/sanitizeFile?jobId=X` (poll until `status=done`) → findings rendered with per-category chips and entity detail rows. Result fields live under `metadata.findings` in the PS response.

**Stored secrets:** User LLM API keys and PS App IDs are Fernet-encrypted before DB storage (`crypto.py`). The `ENCRYPTION_KEY` env var must be a valid Fernet key.

**Audit log:** Config changes (PS settings, LLM keys, user/tenant CRUD) write `AuditEvent` rows alongside chat `Message` rows; both appear in the admin activity log.

### Environment variables that change runtime behavior
- `DB_BACKEND` — `postgres` switches to a local PostgreSQL server; default is file-based SQLite
- `SQLITE_PATH` — SQLite file location (default `app/data/hgapp.db`)
- `DATABASE_URL` — full SQLAlchemy async URL; takes precedence over `DB_BACKEND`
- `SHOW_LLM_KEY_SETTINGS` — shows per-user LLM key fields in the UI
- `APP_ENV` / `ENV` — used for environment detection
- `DEFAULT_DAILY_LIMIT` — per-user message cap (null = unlimited)
- `MAX_FILE_SIZE_MB` — upload size limit (default 10 MB)
- `SANITIZE_MAX_PER_MINUTE` — rate limit for file scans per user (default 5)
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `PERPLEXITY_API_KEY` / `OPENROUTER_API_KEY` — shared provider keys (can also be set via Admin → Settings)

See `.env.example` at the repo root for the complete annotated list.

---

## Demo Scenario Translations

The demo panel supports multi-language PII prompts via a language picker (`<select>`) shown per country when translations exist. Translations use a **dual-write** pattern so existing deployments pick them up without DB reload.

### Architecture

| Layer | Where | How loaded |
|---|---|---|
| DB seed | `app/data/scenarios.json` — `meta.prompt_XX` keys | Fresh seed only (count == 0) |
| Frontend hardcode | `_SCENARIO_TRANSLATIONS` in `app/static/index.html` | Merged at runtime by `_applyBuiltinTranslations()` |
| Registry | `app/data/translations.py` | Imported by tests |

### Language codes (LANG_NAMES)

| Code | Language | Countries |
|---|---|---|
| `en` | English | All |
| `hi` | हिन्दी | IN |
| `he` | עברית | IL |
| `zh` | 中文 | SG |
| `de` | Deutsch | DE |
| `ja` | 日本語 | JP |
| `pt` | Português | BR |
| `ms` | Bahasa Malaysia | MY |

### Adding a translation to an existing country

1. Add `meta.prompt_XX` to the scenario in `app/data/scenarios.json`
2. Add the same entry to `_SCENARIO_TRANSLATIONS[key]` in `app/static/index.html`
3. **Add a PS API test** to `tests/test_pii_translations.py` — parametrize with `(key, lang, expected_entities)`. Also add `(key, lang)` to `_registered_translations()`. **CI fails if a `prompt_XX` key exists without a test.**
4. Entity types to expect: see `tests/fixtures/ps_policy_reference.json` per country code.
5. Source of truth for entity names: `~/Documents/git/prompt_repos/ps-ai-engine/apps/ps-sensitive-data/`

### Adding a new country with translations

Same checklist as above, plus:
- Add `LANG_NAMES[code]` entry to both `app/static/index.html` **and** `app/data/translations.py`
- For injection-type scenarios: assert `action == "block"`; for PII: assert `action == "modify"`
- Countries with English-only (US, AU, GB): no translation needed, no picker shown

### Running PS API tests locally

```bash
export PS_BASE_URL=https://your-tenant.promptsecurity.ai
export PS_APP_ID=your-app-id
pytest tests/test_pii_translations.py -v
```

Without credentials: all PS tests skip (yellow in CI), none fail.

---

## Session Log

- **2026-06-09** — Session log section added; CLAUDE.md pre-existed.
- **2026-06-15** — Multi-country language picker feature: `<select>` dropdowns for 7 PII countries + Prompt Injection; real PS API tests with coverage guard; CLAUDE.md translation guide.
