# HomeGrown App Demo

A multi-user AI chat application with deep [Prompt Security](https://www.prompt.security) integration, built with FastAPI and PostgreSQL. With a lot of blood sweat and tears. 

---

## Features

### Core Chat
- **Multi-user streaming chat** — real-time SSE responses with full session history
- **Per-user daily message limits** — configurable caps to control usage
- **Multiple LLM providers** — OpenAI, Anthropic, Google, Perplexity, and OpenRouter (including free models) called directly with your API keys

### Prompt Security Integration
- **API mode** — explicit prompt and response scanning before and after each LLM call; violations shown as clickable detail cards with full PS response JSON
- **Gateway mode** — all LLM traffic routed through the PS proxy URL; no explicit scan calls, PS intercepts at the network layer
- **PS API inspector** — collapsible panel beneath each violation card shows raw PS request/response JSON, syntax-highlighted
- **File sanitization** — dedicated **🛡️ File Scan** button in the toolbar opens a full-width modal; drag-and-drop or load a built-in example file (PII test PDF) and submit it through the PS `/api/sanitizeFile` two-step async API; results show an action badge, per-category finding chips (e.g. Sensitive Data, Language Detector), and a detailed entity table with type, original value, confidence score, and redacted token for each finding

### Demo & Education
- **Interactive walkthrough** — step-by-step tour showing the exact Python code running at each stage: user input → PS prompt scan → LLM call → PS response scan → display
- **Side-by-side compare mode** — splits the chat into two live columns: left with PS active, right with raw LLM output, so the impact of PS is immediately visible
- **Pre-built demo scenarios** — ready-to-load prompts for PII detection, topic policy, token DoS, and prompt injection, each with Load and Compare buttons
- **API Flow diagram** — custom diagram showing the full request path (User → App + PS Engine → LLM Providers) with bidirectional arrows

### Admin & Audit
- **Admin dashboard** — message volume charts, model distribution, PS action breakdown, top users, per-user detail views
- **User management** — create, edit, and delete users; set per-user daily message limits and model restrictions
- **PS tenant management** — create and manage multiple Prompt Security tenants with separate App IDs and URLs
- **Audit log** — all config changes (PS settings, user/tenant CRUD) appear in the activity log alongside chat messages

---

## Quick Start

The app runs directly with Python — no Docker, no database server. A file-based SQLite database is created automatically on first start.

### Prerequisites

- Python 3.12+

### 1. Clone the repo

```bash
git clone https://github.com/prompt-security/homegrown-ai-app-demo.git
cd homegrown-ai-app-demo
```

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure (optional)

```bash
cp .env.example .env         # then fill in ENCRYPTION_KEY, SECRET_KEY, provider keys, ...
```

The app starts fine without a `.env` — ephemeral keys are generated and a warning is logged. Set real keys before any production-like use.

### 4. Run the app

```bash
cd app
uvicorn main:app --reload --port 8000
```

The SQLite database is created at `app/data/hgapp.db` on first start and persists across restarts.

### 5. Complete initial setup

Open [http://localhost:8000/admin](http://localhost:8000/admin). On first run the admin panel opens directly on the **Settings** page. Work through each section — Security (encryption key, JWT secret, admin password), then any other sections flagged with a red dot — until the nav is clear.

### Open Mode vs User Mode

The app supports two ways to use the chat interface:

| | Open Mode (Guest) | User Mode |
| --- | --- | --- |
| **Login required** | No | Yes |
| **Who uses it** | Walk-up visitors, demo audiences | Named accounts created by admin |
| **Identity** | Optional name + email (or anonymous by IP) | Email + password |
| **PS config** | Supplied per-request in the UI | Saved in user profile |
| **Session history** | Stored by guest ID for that session | Persistent across sessions |
| **Activity logging** | Logged in admin as guest events | Logged in admin under user account |
| **Daily limits** | Not enforced | Enforced per-user if set |

**Open Mode** is ideal for demos and live events, or a hosted instance of this application — visitors can start chatting immediately without creating an account. Prompt Security can still be configured and tested in real time.

**User Mode** is for recurring users who need persistent history, saved PS settings, and usage tracking. Admin creates accounts from the dashboard. This is ideal for local installations that isn't hosted.

Both modes can be active simultaneously — the chat UI shows a identification option while still allowing guest access.

### Available URLs

| URL | Description |
| --- | ----------- |
| [http://localhost:8000](http://localhost:8000) | Chat UI |
| [http://localhost:8000/admin](http://localhost:8000/admin) | Admin dashboard (requires password) |

---

## Database backends

The app runs on **SQLite** (default, zero setup) or **PostgreSQL** - selected by environment variable, no code changes.

| Variable | Effect |
| -------- | ------ |
| *(none)* | File-based SQLite at `app/data/hgapp.db`, created automatically |
| `SQLITE_PATH=/path/to/file.db` | Custom SQLite file location |
| `DB_BACKEND=postgres` | Local PostgreSQL server (`postgresql+asyncpg://hgapp:hgapp_dev@localhost:5432/hgapp`) |
| `DATABASE_URL=...` | Any SQLAlchemy async URL - takes precedence over `DB_BACKEND` |

SQLite runs in WAL mode with foreign-key enforcement on, matching Postgres behaviour, and is well suited to demos, laptops, and single-user installs.

### Using PostgreSQL

For multi-user deployments with concurrent writers, point the app at a Postgres server:

```bash
export DATABASE_URL="postgresql+asyncpg://user:password@host:5432/dbname"
cd app && uvicorn main:app --port 8000
```

(or `DB_BACKEND=postgres` if your local server matches the default credentials above.)

---

## LLM Providers & Models

### Direct provider routing (model discovery)

All LLM calls go directly to the provider's API. When an API key is saved for a supported provider in **Admin → Settings → LLM API Keys**, the app queries that provider's `/models` endpoint and populates the model picker with all available models.

Discovered models use a `provider/model-id` prefix (e.g. `openai/gpt-4.1`, `anthropic/claude-opus-4`) and are called **directly** against the provider's API. This means:

- Any model the provider exposes is instantly available in the UI after saving a key
- Per-user API keys take priority over the shared admin key for that provider
- Models whose provider has no key configured are hidden from the picker

| Provider | Env var / admin key | Discovery source |
| -------- | ------------------- | ---------------- |
| OpenAI | `OPENAI_API_KEY` | `GET /v1/models` (filtered to chat models) |
| Anthropic | `ANTHROPIC_API_KEY` | `GET /v1/models` (falls back to a static known-model list) |
| Google | `GOOGLE_API_KEY` | `GET /v1beta/openai/models` |
| Perplexity | `PERPLEXITY_API_KEY` | `GET /v1/models` |
| OpenRouter | `OPENROUTER_API_KEY` | `GET /v1/models` |

Discovered models are persisted in the database and survive restarts. Re-triggering discovery (by re-saving a key in the admin panel) refreshes the list.

---

## Prompt Security

### Setup

1. Log in as admin and go to **Settings → PS Regions**.
2. Create a region with your PS `base_url` (API mode) and optionally a `gateway_url` (Gateway mode). Both URLs must be public HTTPS hostnames — localhost, `.local`, private IPs, and reserved networks are rejected.
3. In **Settings → Security**, select the region, enter your PS App ID, and choose API or Gateway mode.

### Modes

| Mode | How it works |
| ---- | ------------ |
| **API mode** | The app calls the PS API explicitly before and after each LLM call. Violations are shown as clickable detail cards. |
| **Gateway mode** | All LLM traffic is routed through the PS proxy URL. No explicit scan calls — PS intercepts at the network layer. |

> **Important:** Each PS tenant has its own App ID. If you switch tenants, you must re-enter the App ID for the new tenant. The previous App ID is automatically cleared on tenant change.

---

## Admin Dashboard

Located at `/admin` (admin password required).

| Tab | Description |
| --- | ----------- |
| **Overview** | Message volume chart, model distribution, PS action breakdown, top users |
| **Prompt Security** | PS mode stats, per-mode toggle cards |
| **Users** | User list with per-user stats, inline edit, detail view with charts |
| **Activity Log** | Combined view of all chat messages and config change audit events |
| **Settings** | All configuration — General, Application, Security, Email, PS Regions, LLM Keys; red nav dots flag anything misconfigured |

---

## License

MIT

## Credits
* Original webapp by Carlos Payes
* Contributions by Ori Tabac
* Overhaul, UI enhancements and features by PJ Norris