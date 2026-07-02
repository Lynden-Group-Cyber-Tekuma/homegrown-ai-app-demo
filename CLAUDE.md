# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A multi-user AI chat application built to demonstrate [Prompt Security](https://www.prompt.security) (PS) integration end to end: every prompt and response is scanned by (or proxied through) PS, violations surface in the UI as detail cards, and a demo panel ships ready-made attack scenarios (per-country PII disclosure, prompt injection, system-prompt leak, topic policy, token DoS). Stack: async FastAPI + PostgreSQL + LiteLLM proxy + a no-build vanilla-JS frontend, orchestrated with Docker Compose. The app version lives in `app/VERSION` and is served at `GET /version`.

## Changelog

This project maintains a `CHANGELOG.md` at the repo root in [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format (date-based, no semver).

**Rule:** Whenever you implement a feature, fix, or any meaningful change, add an entry to `CHANGELOG.md` under today's date before committing. Use subsections `### Added`, `### Changed`, `### Fixed`, `### Security` as appropriate. Put the newest date at the top.

**Rule:** Each changelog entry must include the author who owns the change. Use only the username portion of their email (the part before `@`) as an incognito identifier, e.g. `- @johndoe`. If Claude implements the change autonomously, attribute it to the user who requested it.

---

## Commands

### Run with Docker (recommended)

```bash
docker compose up -d                          # start app + db + litellm
docker compose logs -f litellm                # first run only: ~110 Prisma migrations, 3-5 min
docker compose logs -f app                    # app logs
docker compose restart litellm                # after editing litellm/config.yaml
docker compose up -d --build app              # rebuild - only needed after requirements.txt / Dockerfile changes
docker compose --profile ollama up -d ollama  # optional: local Ollama models
docker compose down -v --rmi all              # full reset - wipes DB, users, chat history
```

`./app` is bind-mounted into the container and uvicorn runs with `--reload`, so Python and HTML edits apply live without a rebuild.

| URL | What |
|---|---|
| http://localhost:9100 | Chat UI (host port 9100 maps to container port 8000) |
| http://localhost:9100/admin | Admin dashboard - first-run setup starts here (work through the red-dot Settings sections) |
| http://localhost:4000 | LiteLLM proxy (direct) |

### Run locally without Docker

```bash
docker compose up -d db                        # Postgres only (still via Docker)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt           # includes runtime requirements.txt
export DATABASE_URL="postgresql+asyncpg://hgapp:hgapp_dev@localhost:5432/hgapp"
export LITELLM_BASE_URL="http://localhost:4000"   # optional - only if the litellm container is also up
cd app && uvicorn main:app --reload --port 8000
```

- The built-in defaults for `DATABASE_URL` and `LITELLM_BASE_URL` use the Compose hostnames `db` and `litellm`, which do not resolve outside Docker - the overrides above are required.
- Run uvicorn **from `app/`**: `main.py` mounts `StaticFiles(directory="static")` relative to the working directory.
- `main.py` calls `load_dotenv()`, so a `.env` file in `app/` also works for these variables.
- Without LiteLLM the app still boots and serves the hardcoded `_FALLBACK_MODELS` list; direct provider routing works as soon as a provider key is saved in the admin panel.

### Tests

```bash
pip install -r requirements-test.txt
pytest                                       # whole suite - no Postgres, LiteLLM, or PS needed
pytest tests/test_app_endpoints.py           # single file
pytest tests/test_chat_stream.py::test_name  # single test
```

- `tests/conftest.py` sets `DATABASE_URL=sqlite+aiosqlite:///:memory:` (plus dummy secrets) **before** importing any app module, so the suite is fully self-contained. `pytest.ini` sets `asyncio_mode = auto` and `pythonpath = app`.
- Outbound HTTP is mocked with `respx`; the FastAPI app is exercised in-process via `httpx.ASGITransport`.
- `tests/test_pii_translations.py` holds real PS API integration tests: they skip unless `PS_BASE_URL` and `PS_APP_ID` are exported (yellow in CI, never red). CI passes these from repo secrets.
- Keep new module-level code import-safe under SQLite with no network access - the whole suite depends on it.

### Lint

```bash
ruff check app tests    # what CI runs (non-blocking while the baseline is cleaned up); no repo-level ruff config
```

---

## Architecture

### Service topology (docker-compose.yml)

| Service | Image | Host port | Purpose |
|---|---|---|---|
| `app` | built from `Dockerfile` (python:3.12-slim) | 9100 | FastAPI backend + static frontend |
| `db` | postgres:16-alpine | 5432 | Two databases: `hgapp` (app) and `litellm` - `postgres/init.sql` creates the second so LiteLLM's ~110 Prisma migrations never touch app tables |
| `litellm` | ghcr.io/berriai/litellm:main-stable | 4000 | OpenAI-compatible proxy for the models declared in `litellm/config.yaml` |
| `ollama` | ollama/ollama (Compose profile `ollama`, off by default) | 11434 | Optional local models |

Details that matter:

- The app container starts as root (`user: "0"`): `docker-entrypoint.sh` joins `appuser` to whatever group owns the mounted Docker socket, chowns `/app/data`, then drops privileges via `gosu`. The Docker socket is mounted so the admin UI can create/start/stop the Ollama container itself (Docker SDK, `_get_docker_client()` in `main.py`).
- `litellm/config.yaml` sets `drop_params: true`, `request_timeout: 60` and `ssl_verify: false` (plus `PYTHONHTTPSVERIFY=0`) to tolerate SSL-inspecting corporate networks; `certs/corporate-ca.pem` is mounted into the Ollama container for the same reason.
- `app/data/` holds runtime state written by the app: the `scenarios.json` seed file plus the `db_config_override.json` / `crypto_config_override.json` override files (see Configuration below).

### Backend layout

**Single-file FastAPI backend** - all 88 routes live in `app/main.py` (~4,600 lines). There are no separate routers. Supporting modules are deliberately thin:

| Module | Contents |
|---|---|
| `models.py` | Async SQLAlchemy ORM: `PSTenant`, `User`, `ChatSession`, `Message`, `AuditEvent`, `AppSetting` (generic key/value store), `DemoScenario`, `APIKey` |
| `schemas.py` | Pydantic v2 request/response types |
| `auth.py` | JWT issue/verify, bcrypt passwords, API-key hashing (HMAC-SHA256 keyed with `SECRET_KEY`), `get_current_user` / `require_admin` dependencies |
| `crypto.py` | Fernet encryption for secrets at rest, plus hot-swap and override-file helpers |
| `database.py` | Async engine + `get_db` dependency; supports a URL override file and in-process engine rebuild |
| `prompt_security.py` | `PromptSecurityClient`: `POST /api/protect` (prompt/response scans) and the two-step `POST`/`GET /api/sanitizeFile` job flow |
| `token_counter.py` | Token estimation via LiteLLM tokenizers with a chars/4 fallback |
| `data/translations.py` | Registry of demo-scenario translations (imported by tests) |
| `static/` | Frontend: `index.html` (chat, ~7k lines), `admin.html` (dashboard, ~5k lines), `login.html`. Self-contained inline JS/CSS, vendored libs in `static/vendor/` (marked, DOMPurify, highlight.js). No build step, no npm |

Sessions and messages carry either a `user_id` (user mode) or a `guest_id` (open mode). `Message` rows store PS results (`ps_action`, `ps_violations` JSON) and token counts - the entire admin dashboard is aggregated from them.

### Startup sequence (lifespan in main.py)

1. `_validate_security_bootstrap_config()` - when `APP_ENV` is not dev/test/local, refuses to boot with a missing or default `SECRET_KEY` / `ADMIN_PASSWORD`.
2. `Base.metadata.create_all` plus a list of idempotent `ALTER TABLE` statements. **This is the entire migration system** - Alembic is in requirements but unused. A new column must be added both to `models.py` and to that ALTER list (for existing deployments).
3. If no admin exists, creates a placeholder admin `setup-<hex>@wizard.internal` with a random password and `must_change_password=True`; the Setup Wizard takes over from there.
4. Seeds `demo_scenarios` from `app/data/scenarios.json` **only when the table is empty**.
5. Loads DB-stored settings over env defaults: JWT secret, LiteLLM master key, shared provider keys, daily limit, max file size, Ollama config, previously discovered models.
6. `refresh_model_cache()`.

### Configuration and secrets layering

Environment variables are only *seeds* - nearly everything is runtime-configurable in Admin -> Settings and persisted in the `app_settings` table (Fernet-encrypted where secret). Effective precedence, highest first:

1. Override files in `app/data/` (`crypto_config_override.json`, `db_config_override.json`) - read at module import time
2. `app_settings` DB rows - loaded in lifespan and **hot-swapped in-process** when changed through admin endpoints (`set_secret_key`, `set_encryption_key`, `set_litellm_master_key`, `rebuild_engine`) - no restart needed
3. Environment variables
4. Hardcoded defaults

`ENCRYPTION_KEY` must be a valid Fernet key (generate with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). When missing or invalid, an ephemeral key is generated with a warning: everything works until restart, after which previously encrypted DB values (PS App IDs, LLM keys, JWT secret, SMTP password) become unreadable. Changing the key has the same effect - users must re-enter their secrets.

Setup gating: `GET /setup/status` reports `needs_setup`; `POST /setup/bootstrap-token` (no auth) issues a 2-hour admin JWT until admin password + JWT secret + at least one LLM source are configured, then returns 403 permanently.

### LLM routing

`_user_llm_client()` (and `_guest_llm_client()` for open mode) choose the client per request, in priority order:

1. **Per-user provider key** (encrypted JSON in `User.llm_api_keys_enc`) -> `AsyncOpenAI` direct against the provider URL, with the `provider/` prefix stripped from the model ID.
2. **Discovered `provider/model` ID + shared key** -> direct provider call, bypassing LiteLLM entirely.
3. **Everything else** -> LiteLLM proxy (`AsyncOpenAI(base_url=LITELLM_BASE_URL + "/v1")`). Shared keys saved at runtime are injected per request via `extra_body={"api_key": ...}` (`_litellm_extra`), so config-file models pick up admin-saved keys without a LiteLLM restart.

Exception: `ollama/*` models always bypass LiteLLM and hit Ollama's OpenAI-compatible endpoint directly - LiteLLM does not propagate client disconnects upstream, which previously left Ollama inference burning CPU after the browser tab closed.

The model picker is fed from `_model_cache`, rebuilt by `refresh_model_cache()` from three sources: LiteLLM `/v1/models`, Ollama `/api/tags` (anything `ollama pull`ed appears automatically - `config.yaml` routes it via an `ollama/*` wildcard), and `_DISCOVERED_MODELS`. Saving a provider key in Admin -> Settings triggers `_run_discovery()` in the background: it queries the provider's `/models` endpoint (OpenAI, Anthropic, Google, Perplexity, OpenRouter), prefixes the IDs (`openai/gpt-4.1`), persists them to `app_settings.discovered_models`, and refreshes the cache. If LiteLLM is unreachable, the hardcoded `_FALLBACK_MODELS` list keeps the UI usable.

### Chat streaming flow

`POST /chat/stream` (authenticated) and `POST /guest/chat/stream` (open mode) return Server-Sent Events. Two PS modes, selected per user (or per guest request):

**API mode** (`ps_mode="api"`) - explicit scan sandwich:

1. `PromptSecurityClient.protect_prompt()` -> `block` ends the stream with a `blocked` event; `modify` substitutes the sanitised prompt before the LLM sees it.
2. LLM streamed via the routing above; chunks forwarded as `token` events.
3. `protect_response()` on the full reply -> `block` emits `revoke` (the UI retracts already-rendered text); `modify` emits `sanitized` with the replacement text.
4. `done` carries `ps_action`, violations, token counts, daily-limit usage, and the raw PS request/response JSON (`ps_raw`) that powers the UI's PS API inspector.

**Gateway mode** (`ps_mode="gateway"`) - no explicit scan calls; the LLM call itself is pointed at the PS gateway URL (`PSTenant.gateway_url`) and PS intercepts at the network layer. Three provider paths: OpenAI-compatible (`AsyncOpenAI` with a swapped `base_url`), Gemini native (`:generateContent`), Anthropic native (`/v1/messages`). Requires the provider's LLM key; PS blocks surface as 400/403 responses that are mapped back to `blocked` events.

SSE event types: `token`, `sanitized`, `blocked`, `revoke`, `error`, `done`.

Also handled inside the same endpoint: daily message limit (429 with usage detail), per-user `allowed_models` enforcement (403), session auto-create titled from the first message, and persistence of both user and assistant `Message` rows. `skip_ps` in the request powers the UI's **compare mode** (side-by-side PS-protected vs raw LLM output) and is deliberately available to every authenticated user, not just admins.

**Streaming cancellation**: a background pump task feeds chunks into an `asyncio.Queue`; the generator drains with a 0.4 s timeout and checks `request.is_disconnected()` on each timeout, so closing the tab cancels the upstream inference within about a second. Preserve this pattern when touching the stream loop.

### File scan flow

`POST /upload/sanitize` (authenticated) and `POST /guest/upload/sanitize` -> `PromptSecurityClient.sanitize_file()`: `POST /api/sanitizeFile` (multipart) returns a `jobId`, then `GET /api/sanitizeFile?jobId=X` is polled every 5 s (60 s cap) until `status != "processing"`. Findings live under `metadata.findings` in the PS response; `_build_entity_contexts()` pairs each finding with surrounding text pulled by `_extract_file_text()` (lazy imports: pypdf, python-docx, python-pptx, openpyxl, xlrd). In-memory per-user rate limits: `SANITIZE_MAX_PER_MINUTE` (default 5) and `SANITIZE_MAX_CONCURRENT_PER_USER` (default 1).

Separately, `POST /upload` handles chat attachments: images become base64 data URLs (for vision models), PDFs are converted to text via pypdf, and txt/md/csv/json pass through as text. All uploads are capped at `MAX_FILE_SIZE_MB` (default 10).

### Open mode vs user mode

Two parallel front doors over the same flows:

- **User mode**: JWT auth (`Authorization: Bearer` header, plus an `hgapp_session` httpOnly cookie for page-level auth). PS config (tenant, App ID, mode) is stored per user, encrypted. Daily limits and model restrictions are enforced.
- **Open/guest mode**: `/guest/*` endpoints, no auth. PS config arrives *per request* from browser localStorage; guests identify with an optional name/email (optionally verified via SMTP email codes) or fall back to IP. No daily limits. Activity is still logged, with `user_email` values like `"Name (1.2.3.4) [open mode]"`.
- **Admin**: exactly one `role="admin"` user. `/auth/admin-login` is password-only, checked against `app_settings.admin_password_hash` (falling back to the `ADMIN_PASSWORD` env var). The dashboard at `/admin` is a separate password-protected page - the chat UI has no admin-role concept.
- **API keys**: self-service `hg_live_*` keys (`/users/me/api-keys`), hashed with HMAC-SHA256 keyed by `SECRET_KEY`; legacy unsalted SHA-256 hashes upgrade transparently on first use.

PS tenant URLs are SSRF-guarded (`_validate_external_https_url`): HTTPS only, public hostnames only - localhost, `.local`, private and reserved IP ranges are rejected.

### Audit log

Config changes (PS settings, LLM keys, user/tenant/scenario CRUD, logins) write `AuditEvent` rows via `_log_audit()`; chat history comes from `Message` rows. `/admin/activity` merges both into the dashboard's activity feed. Frontend-originated events go through `/users/log-event` and `/guest/log-event`, restricted to allowlisted event types.

### Demo scenarios

Seeded from `app/data/scenarios.json` into the `demo_scenarios` table (empty-table seed only). Admin CRUD lives at `/admin/demo-scenarios`; `save-master` writes the DB state back to `scenarios.json`, and `sync` imports scenarios from a URL. Because existing deployments never re-seed, scenario content changes must go through the admin editor/sync - or, for translations, the dual-write pattern below.

---

## Environment variables

All of these are optional in development (Docker Compose and code defaults cover them). Values saved through Admin -> Settings override the corresponding env var after first save.

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://hgapp:hgapp_dev@db:5432/hgapp` | Async SQLAlchemy URL |
| `ENCRYPTION_KEY` | none (ephemeral key generated) | Fernet key for secrets at rest - set it, or encrypted values die on restart |
| `SECRET_KEY` | none (ephemeral, replaced from DB) | JWT signing + API-key HMAC |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | `admin@sentinelone.com` / `ChangeMe!` | Admin fallback credentials; production boot fails on defaults |
| `APP_ENV` / `ENV` | `development` | Non-dev values enable the production security bootstrap checks |
| `TOKEN_TTL_H` | `24` | JWT lifetime in hours |
| `LITELLM_BASE_URL` | `http://litellm:4000` | LiteLLM proxy address |
| `LITELLM_MASTER_KEY` | empty | Auth for the LiteLLM proxy |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `PERPLEXITY_API_KEY`, `OPENROUTER_API_KEY` | empty | Shared provider keys (seed values; admin-saved keys override and trigger model discovery) |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama endpoint for direct calls and model discovery |
| `DEFAULT_DAILY_LIMIT` | `50` | Per-user daily message cap (`0` = unlimited) |
| `MAX_FILE_SIZE_MB` | `10` | Upload size cap |
| `SANITIZE_MAX_PER_MINUTE` | `5` | File scans per user per minute |
| `SANITIZE_MAX_CONCURRENT_PER_USER` | `1` | Concurrent file scans per user |
| `SHOW_LLM_KEY_SETTINGS` | `false` | Shows per-user LLM key fields in the UI |
| `SMTP_HOST`, `SMTP_PORT`, `EMAIL_USERNAME`, `EMAIL_PASSWORD`, `FROM_EMAIL` | empty | Guest email-code verification (DB-saved settings take precedence) |
| `LOCAL_OPENAI_BASE_URL` / `LOCAL_OPENAI_API_KEY` | unset | On the **litellm** service: local OpenAI-compatible endpoint (see the `huggingface/...` entry in config.yaml) |

---

## CI

- `.github/workflows/ci.yml` - pytest on Python 3.11 and 3.12. Repo secrets `PS_BASE_URL` / `PS_APP_ID` enable the real PS integration tests; without them those tests skip.
- `.github/workflows/security.yml` - ruff (non-blocking), Semgrep -> SARIF, pip-audit (non-blocking), gitleaks (non-blocking), Trivy filesystem scan, dependency-review on PRs.
- `.github/workflows/codeql.yml` - CodeQL analysis.

---

## Demo Scenario Translations

The demo panel supports multi-language PII prompts via a language picker (`<select>`) shown per country when translations exist. Translations use a **dual-write** pattern so existing deployments pick them up without DB reload.

### Architecture

| Layer | Where | How loaded |
|---|---|---|
| DB seed | `app/data/scenarios.json` - `meta.prompt_XX` keys | Fresh seed only (count == 0) |
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
3. **Add a PS API test** to `tests/test_pii_translations.py` - parametrise with `(key, lang, expected_entities)`. Also add `(key, lang)` to `_registered_translations()`. **CI fails if a `prompt_XX` key exists without a test.**
4. Entity types to expect: see `tests/fixtures/ps_policy_reference.json` per country code.
5. Source of truth for entity names: `~/Documents/git/prompt_repos/ps-ai-engine/apps/ps-sensitive-data/`

### Adding a new country with translations

Same checklist as above, plus:

- Add a `LANG_NAMES[code]` entry to both `app/static/index.html` **and** `app/data/translations.py`
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

## Gotchas

- The host port is **9100**, not 8000 or 9000 (`.codex/skills/homegrown-ai-app-demo/SKILL.md` still says 9000 - it is stale; docker-compose.yml is the source of truth).
- LiteLLM's first boot runs ~110 Prisma migrations (3-5 minutes). The app comes up first; models appear once LiteLLM is ready (`POST /admin/refresh-models` forces a refresh).
- Editing `app/data/scenarios.json` does nothing for an existing database - seeding only happens when `demo_scenarios` is empty.
- There is no Alembic migration tree: schema changes go in `models.py` **and** in the idempotent `ALTER TABLE` list inside `lifespan()` in `main.py`.
- Changing or losing `ENCRYPTION_KEY` silently invalidates every Fernet-encrypted DB value; users must re-enter PS App IDs and LLM keys.
- `database.py` and `crypto.py` read their override files at import time - tests rely on `conftest.py` setting env vars before any app import; keep it that way.
- The frontend is three large hand-maintained HTML files with inline JS. There is no bundler, framework, or frontend lint - match the existing style and keep vendored libraries in `app/static/vendor/`.

---

## Session Log

- **2026-06-09** - Session log section added; CLAUDE.md pre-existed.
- **2026-06-15** - Multi-country language picker feature: `<select>` dropdowns for 7 PII countries + Prompt Injection; real PS API tests with coverage guard; CLAUDE.md translation guide.
- **2026-07-02** - CLAUDE.md rewritten as a full architecture and operations guide: service topology, startup and configuration layering, LLM routing tiers, chat/file-scan flows, corrected local (non-Docker) run instructions, environment variable reference, CI overview, gotchas.
