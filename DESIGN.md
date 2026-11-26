# Design Document: Slim Todoist MCP & Mini-Backend (`autodoist`)

## 1. Objective & Scope

Build a lightweight personal backend that sits between **Gemini** (or another OAuth 2.1 MCP client) and **Todoist**, exposing a narrow, opinionated **MCP (Model Context Protocol)** surface. Todoist remains the UI, mobile widget, reminder engine, and spouse-sharing layer; this service provides low-token LLM tooling, code-enforced guardrails, post-hoc activity logging, and long-term completion memory.

### Goals

- **Low context overhead:** Expose 5 workflow-oriented MCP tools instead of Todoist's full ~47-tool surface.
- **Code-enforced guardrails:** Prevent destructive actions (deleting tasks/projects, creating new projects) and restrict accessible projects/labels in server code rather than relying on prompt compliance.
- **Unlimited completion history:** Incrementally sync completions into a local SQLite database for long-term (up to 365-day) recurrence audits.
- **Native post-hoc logging:** Allow logging completed real-world actions (*"just bought diapers"*) directly into history without cluttering active Todoist lists.
- **Standard OIDC / OAuth 2.1 + static Todoist token:** Use a static personal API token for Todoist and any standard OIDC provider (passkeys, CIMD, etc.) for the MCP client.

### Non-Goals

- Building a custom web/mobile frontend, notification engine, or recurring-date scheduler.
- Multi-tenant Todoist isolation in v1 (scoped to one household's Todoist account, while enforcing `sub` allowlisting).
- Real-time Todoist webhooks (periodic in-process polling is sufficient).

---

## 2. Architecture & Tech Stack

| Layer | Choice | Rationale |
| --- | --- | --- |
| **Language / Runtime** | Python 3.12+ (`hatchling` package) | Clean MCP ecosystem, typed `src/autodoist/` package with `py.typed`. |
| **MCP Framework** | `fastmcp` (Stateless JSON HTTP) | `RemoteAuthProvider` + `JWTVerifier` for stateless JWKS verification; `stateless_http=True, json_response=True` avoids reverse-proxy SSE buffering and restart session loss. |
| **Identity Provider** | Any OIDC / OAuth 2.1 provider (e.g. Pocket ID with CIMD) | Configured via `auth.oidc_issuer`, optional `oidc_jwks_uri`, `jwt_algorithm`, fail-closed `jwt_audience`, and `allowed_user_subs`. |
| **Todoist Client** | `httpx` (`<1.0`) against **Todoist API v1** (`https://api.todoist.com/api/v1`) | Unified surface covering REST resources (`/tasks`, `/projects`, `/activities`) and `/sync` batch/incremental commands. |
| **Database** | SQLite (WAL mode, persistent Docker volume / bind mount) | Zero-ops embedded storage for `sync_state` and `completion_log`, offloaded via `asyncio.to_thread`. |
| **Deployment / Scheduler** | Docker Compose + in-process `sync_loop` | Runs behind an HTTPS reverse proxy (Synology DSM / Caddy / Nginx); background sync runs in-process on `sync.interval_minutes`, with `python -m autodoist.sync_worker` for manual runs. |
| **CI / CD** | GitHub Actions (`ruff`, `pyrefly`, `pytest`, `docker/build-push-action`) | Dedicated lint/typecheck and test jobs gate multi-arch GHCR builds (`ghcr.io/<owner>/autodoist`). |

### Repository Layout

```text
autodoist/
├── .github/workflows/docker.yml   # lint (ruff + pyrefly), test (pytest), & GHCR multi-arch build
├── DESIGN.md
├── config.example.toml            # Non-secret config template (copy to config.toml)
├── .env.example                   # Secrets template (TODOIST_API_TOKEN, optional AUTH_DISABLED)
├── pyproject.toml                 # hatchling package, dependencies, ruff/pytest/pyrefly config
├── Dockerfile                     # Python 3.12-slim container with --proxy-headers
├── compose.yaml                   # Persistent volume + read-only config.toml mount + healthcheck
├── src/autodoist/
│   ├── __init__.py
│   ├── py.typed
│   ├── config.py                  # TOML AppConfig + EnvSettings + fail-closed validation
│   ├── models.py                  # Typed Todoist boundary models & MCP tool input/output schemas
│   ├── db.py                      # SQLite schema, completion reconciliation, & post-hoc lookup
│   ├── todoist.py                 # Typed Todoist API v1 client & TTL ProjectCache
│   ├── sync_worker.py             # In-process periodic sync_loop + CLI entrypoint
│   ├── service.py                 # AutodoistService domain layer (guardrails & async DB offload)
│   └── server.py                  # Thin FastMCP server, OIDC authz middleware, & /health
└── tests/
    ├── test_config_db.py
    ├── test_todoist_sync.py
    └── test_guardrails.py
```

---

## 3. Authentication & Security

1. **Backend → Todoist:**
   - Static **Personal API Token** via `TODOIST_API_TOKEN` (`Authorization: Bearer <token>`), validated at startup.
2. **MCP Client → Backend (OIDC / OAuth 2.1):**
   - `RemoteAuthProvider` advertises `auth.oidc_issuer` (preserving exact RFC 8414 URL without trailing slash) at `/.well-known/oauth-protected-resource/mcp`.
   - `JWTVerifier` verifies Bearer JWT signatures against `oidc_jwks_uri` (defaulting to `<oidc_issuer>/.well-known/jwks.json`), `issuer`, asymmetric `jwt_algorithm` (default `RS256`, rejecting `none`/`HS*`), and required `jwt_audience`.
   - `_AllowedUserASGIMiddleware` rejects valid JWTs whose `sub` is not in `auth.allowed_user_subs` with HTTP `403`, backed by per-tool `_require_authorized_user()` defense-in-depth.
   - Startup fails closed unless `mcp_base_url`, `oidc_issuer`, `jwt_audience`, `allowed_user_subs`, and `TODOIST_API_TOKEN` are configured (unless `AUTH_DISABLED=true` for local dev).

---

## 4. Local Data Model (SQLite)

```sql
CREATE TABLE sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sync_token TEXT NOT NULL DEFAULT '*',
    last_synced_at TIMESTAMP NOT NULL
);

CREATE TABLE completion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todoist_task_id TEXT,
    content TEXT NOT NULL,
    project_name TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '[]',
    completed_at TIMESTAMP NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('todoist_sync', 'post_hoc_log', 'mcp_complete')),
    UNIQUE(todoist_task_id, completed_at)
);

CREATE INDEX idx_completion_log_completed_at ON completion_log(completed_at);
```

- When `todoist_sync` receives a completion for a task already logged via `mcp_complete` within `±60s`, it reconciles that row in-place to Todoist's authoritative server timestamp and `source='todoist_sync'`.
- Synced items missing a Todoist `completed_at` timestamp are skipped rather than stamped with container time.

---

## 5. MCP Tool Surface (5 Tools)

| Tool Name | Purpose | Inputs | Guardrails & Backend Behavior |
| --- | --- | --- | --- |
| `get_tasks_by_synergy` | Structured retrieval for daily triage or batching. | `project?`, `label?`, `include_overdue` | Queries active tasks, filters to `allowed_projects`, evaluates overdue dates in `rules.timezone`, groups by `synergy_labels` / `untagged`, and sorts by priority/due date. |
| `capture_items` | Batch intake for tasks and post-hoc logs. | `items`: list of `{ content, mode, project, labels, due_string?, priority?, notes? }` | Validates `capture_projects` & `allowed_labels`. **Tasks:** skips normalized duplicates against active project tasks, auto-attaches `review_label`, submits batched `/sync item_add`. **Post-hoc:** skips duplicates within-batch and against recent (`300s`) `post_hoc_log` entries, then writes to `completion_log`. |
| `update_or_reschedule` | Safe edits and recurrence-preserving date moves. | `task_id`, `new_due_date?`, `labels?`, `priority?`, `project?`, `clear_review_flag` | Validates `task_id`, `YYYY-MM-DD`, `allowed_projects`, and `allowed_labels`; preserves `review_label` unless `clear_review_flag=true`; preserves recurrence string on recurring tasks. |
| `complete_task` | Marks a task done and immediately records it. | `task_id` | Verifies task is in `allowed_projects`, closes via Todoist API, and records `mcp_complete` row (later reconciled on sync). |
| `get_completion_history` | Feeds weekly recurrence audit. | `days` (`1..365`, default `90`), `project?` | Queries `completion_log` scoped to `allowed_projects` and returns compact `(content, project_name, completed_at)` records. |

---

## 6. Incremental Sync Strategy

1. `sync_loop` runs inside FastMCP's lifespan immediately on startup and every `sync.interval_minutes` (`0` disables it). Manual runs use `python -m autodoist.sync_worker`.
2. Each run refreshes `ProjectCache` once (`GET /projects`), calls `/sync` (`resource_types=["items"]`) with the stored `sync_token`, and performs a best-effort `GET /activities` (`object_type=item, event_type=completed`) pull to capture recurring task completions (gracefully skipping on HTTP `402`/`403`).
3. Completed items in `allowed_projects` are written in a single off-thread SQLite transaction, and the new `sync_token` is persisted only after ingestion succeeds.
