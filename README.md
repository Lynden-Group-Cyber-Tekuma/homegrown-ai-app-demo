# HomeGrown App Demo

A multi-user AI chat application with deep [Prompt Security](https://www.prompt.security) integration, built with FastAPI, PostgreSQL, and LiteLLM. With a lot of blood sweat and tears. 

---

## Features

### Core Chat
- **Multi-user streaming chat** — real-time SSE responses with full session history
- **Per-user daily message limits** — configurable caps to control usage
- **Multiple LLM providers** — OpenAI, Anthropic, Google, Perplexity, and OpenRouter (including free models) via LiteLLM

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

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose

### IMPORTANT — Upgrading from a previous version?

Due to backend changes, remove old containers, volumes, and images before starting fresh:

```bash
docker compose down -v --rmi all
```

This stops all containers, deletes the named volumes (database, app data), and removes locally-built images. Your `docker-compose.yml` and config files are not touched.

> **Note:** This will erase all chat history, users, and settings stored in the database. Export anything you need first.


### 1. Clone the repo

```bash
git clone https://github.com/prompt-security/homegrown-ai-app-demo.git
cd homegrown-ai-app-demo
```

### 2. Build and Start all services

```bash
docker compose up -d --build
```

> **Note:** On first run, LiteLLM applies ~110 database migrations. This takes 3–5 minutes. Subsequent starts are instant. Check progress with:
>
> ```bash
> docker compose logs -f
> ```

### 3. Complete initial setup

Open [http://localhost:9100/admin](http://localhost:9100/admin). On first run the admin panel opens directly on the **Settings** page. Work through each section — Security (encryption key, JWT secret, admin password), then any other sections flagged with a red dot — until the nav is clear.

### 4. Open Mode vs User Mode

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

### 5. Available URLs

| URL | Description |
| --- | ----------- |
| [http://localhost:9100](http://localhost:9100) | Chat UI |
| [http://localhost:9100/admin](http://localhost:9100/admin) | Admin dashboard (requires password) |
| [http://localhost:4000](http://localhost:4000) | LiteLLM proxy (direct) |

---

## LLM Providers & Models

The app supports two routing paths for LLM calls:

### LiteLLM (proxy)

A LiteLLM proxy runs as a separate Docker service on port 4000. Models listed in `litellm/config.yaml` are served through it. The following are enabled by default:

| Model | Provider | Notes |
| ----- | -------- | ----- |
| `gpt-4o` / `gpt-4o-mini` / `gpt-5-nano` | OpenAI | Requires `OPENAI_API_KEY` |
| `claude-sonnet-4-5-20250929` | Anthropic | Requires `ANTHROPIC_API_KEY` |
| `sonar` | Perplexity | Requires `PERPLEXITY_API_KEY` |
| `gemini-2.0-flash` / `gemini-1.5-pro` | Google via OpenRouter | Requires `OPENROUTER_API_KEY` |
| `meta-llama/llama-3.3-70b-instruct:free` | OpenRouter | **Free** |
| `meta-llama/llama-3.1-8b-instruct:free` | OpenRouter | **Free** |
| `deepseek/deepseek-r1:free` | OpenRouter | **Free** |
| `qwen/qwen-2.5-72b-instruct:free` | OpenRouter | **Free** |
| `qwen/qwen3-next-80b-a3b-instruct:free` | OpenRouter | **Free** |
| `qwen/qwen3-coder:free` | OpenRouter | **Free** |
| `mistralai/mistral-7b-instruct:free` | OpenRouter | **Free** |
| `microsoft/phi-3-mini-128k-instruct:free` | OpenRouter | **Free** |
| `nvidia/nemotron-nano-9b-v2:free` | OpenRouter | **Free** |
| `nvidia/nemotron-3-super-120b-a12b:free` | OpenRouter | **Free** |
| `minimax/minimax-m2.5:free` | OpenRouter | **Free** |
| `stepfun/step-3.5-flash:free` | OpenRouter | **Free** |
| `liquid/lfm-2.5-1.2b-thinking:free` | OpenRouter | **Free** |
| `nousresearch/hermes-3-llama-3.1-405b:free` | OpenRouter | **Free** |
| `bytedance/seedance-1-5-pro` | OpenRouter | Requires `OPENROUTER_API_KEY` |
| `sourceful/riverflow-v2-fast-preview` | OpenRouter | Requires `OPENROUTER_API_KEY` |
| `huggingface/Qwen3VL-8B-Instruct-F16` | Local OpenAI-compatible | See [Local endpoint](#local-openai-compatible-endpoint) |

To add or remove models, edit `litellm/config.yaml` and restart the `litellm` service:

```bash
docker compose restart litellm
```

### Direct provider routing (model discovery)

When an API key is saved for a supported provider in **Admin → Settings → LLM API Keys**, the app queries that provider's `/models` endpoint and populates the model picker with all available models — no changes to `litellm/config.yaml` needed.

Discovered models use a `provider/model-id` prefix (e.g. `openai/gpt-4.1`, `anthropic/claude-opus-4`) and are called **directly** against the provider's API, bypassing LiteLLM entirely. This means:

- Any model the provider exposes is instantly available in the UI after saving a key
- The LiteLLM proxy is not involved in these calls
- Per-user API keys take priority over the shared admin key for that provider

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

## Local OpenAI-compatible endpoint

For local servers that expose OpenAI-style `/v1/chat/completions` endpoints, add to the `litellm` service `environment` block in `docker-compose.yml`:

```yaml
LOCAL_OPENAI_BASE_URL: "http://host.docker.internal:8081/v1"
LOCAL_OPENAI_API_KEY: "local-dev-key"
```

The default `litellm/config.yaml` includes the `huggingface/Qwen3VL-8B-Instruct-F16` model pointing to this endpoint.

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