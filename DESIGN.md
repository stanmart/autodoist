# Design Document: Slim Todoist MCP & Mini-Backend (`autodoist`)

## 1. Objective & Scope

Build a lightweight personal backend that sits between **Gemini** (or another OAuth 2.1 MCP client) and **Todoist**, exposing a narrow, opinionated **MCP (Model Context Protocol)** surface. Todoist remains the UI, mobile widget, reminder engine, and spouse-sharing layer; this service provides low-token LLM tooling, code-enforced guardrails, post-hoc activity logging, and long-term completion memory.

### Goals

- **Low context overhead:** Expose 6 workflow-oriented MCP tools instead of Todoist's full ~47-tool surface.
- **Code-enforced guardrails:** Prevent destructive actions (deleting or creating projects; tasks can only be deleted one at a time via `drop_task`, which snapshots them first) and restrict accessible projects/labels in server code rather than relying on prompt compliance.
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
│   ├── instructions.py            # MCP server instructions rendered from [rules]
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

CREATE TABLE dropped_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todoist_task_id TEXT NOT NULL,
    content TEXT NOT NULL,
    project_name TEXT NOT NULL,
    task_json TEXT NOT NULL,
    dropped_at TIMESTAMP NOT NULL
);
```

- When `todoist_sync` receives a completion for a task already logged via `mcp_complete` within `±60s`, it reconciles that row in-place to Todoist's authoritative server timestamp and `source='todoist_sync'`.
- Synced items missing a Todoist `completed_at` timestamp log a warning and fall back to sync time (`timestamp_estimated=True`), and are skipped if that `todoist_task_id` already has a row in `completion_log`.

---

## 5. MCP Tool Surface (6 Tools)

| Tool Name | Purpose | Inputs | Guardrails & Backend Behavior |
| --- | --- | --- | --- |
| `get_tasks` | Structured retrieval for briefs, triage, slots, and reviews. | `project?`, `label?`, `due`, `slot?`, `needs_triage`, `older_than_days?`, `group_by`, `limit` | Queries active tasks, filters to `allowed_projects`, evaluates due dates in `rules.timezone`, hides `someday_label` tasks unless requested by `label`, sorts by priority/due date, applies `limit` (reporting `total`/`truncated`), then groups by first synergy label, project, due bucket, or not at all. |
| `capture_items` | Batch intake for tasks and post-hoc logs. | `items`: list of `{ content, mode, project, labels, due_string?, priority?, notes? }` | Validates `capture_projects` & `allowed_labels`. **Tasks:** skips normalized duplicates against active project tasks, auto-attaches `review_label`, submits batched `/sync item_add`. **Post-hoc:** skips duplicates within-batch and against recent (`300s`) `post_hoc_log` entries, then writes to `completion_log`. |
| `update_or_reschedule` | Rewrites, safe edits, and schedule changes. | `task_id`, `content?`, `notes?`, `new_due_date?`, `due_string?`, `labels?`, `priority?`, `project?`, `clear_review_flag` | Validates `task_id`, `YYYY-MM-DD`, `allowed_projects`, and `allowed_labels`; `due_string` (exclusive with `new_due_date`) replaces the whole schedule including recurrence, and `no date` clears it; `labels` replaces only allowlisted labels and keeps hand-set ones; preserves `review_label` unless `clear_review_flag=true`; preserves the recurrence string and the time of day (floating or fixed-timezone) when moving a date. |
| `complete_task` | Marks a task done and immediately records it. | `task_id` | Verifies task is in `allowed_projects`, closes via Todoist API, and records `mcp_complete` row (later reconciled on sync). An already completed task is returned without closing or logging again. |
| `drop_task` | Removes a task that will not be done. | `task_id` | Verifies task is in `allowed_projects`, refuses tasks with subtasks, snapshots the task into `dropped_tasks`, then deletes it in Todoist (the snapshot is discarded if the delete fails). Never touches `completion_log`. |
| `get_completion_history` | Feeds weekly recurrence audit. | `days` (`1..365`, default `90`), `project?` | Queries `completion_log` scoped to `allowed_projects` and returns compact `(content, project_name, completed_at)` records. |

**Server instructions.** `instructions.py` renders the MCP `instructions` from `[rules]`, so the vocabulary the agent needs (projects, label meanings, slots and their capacity) has one source of truth and travels with the server instead of living in a per-provider skill. It deliberately stops at vocabulary: tool-level invariants (complete versus drop, `post_hoc`) sit in the tool descriptions, and procedures such as triage or a weekly review are left to client-side skills, which load on demand and are specific to one household's workflow. `python -m autodoist.instructions` prints the text for clients that ignore MCP instructions.

**Routines.** A routine (`[[rules.routines]]`) is a recurring slot for working through tasks, such as a weekday evening or a weekend block. Its `name` doubles as the label that assigns a task to it, so each task has a *when* (slot label) alongside its *where/how* (synergy label). `get_tasks(slot=...)` returns the slot's labelled tasks plus, when `due_within_days` is set, any task due inside that window. Slot times are deliberately not configured here: they live in the calendar, which the assistant reads separately.

**Project names are not unique in Todoist.** The allowlist is name-based, so the project cache tracks names shared by several active projects as ambiguous: they never resolve to an ID, tool calls touching them raise an error asking for a rename, and background sync skips them with a warning.

**Batch size.** `/sync` accepts at most 100 commands per request, so `capture_items` batches are split into consecutive requests of up to 100 `item_add` commands.

---

## 6. Incremental Sync Strategy

1. `sync_loop` runs inside FastMCP's lifespan immediately on startup and every `sync.interval_minutes` (`0` disables it). Manual runs use `python -m autodoist.sync_worker`.
2. Each run refreshes `ProjectCache` once (`GET /projects`), calls `/sync` (`resource_types=["items"]`) with the stored `sync_token`, and performs a best-effort `GET /activities` (`object_type=item, event_type=completed`) pull to capture recurring task completions (gracefully skipping on HTTP `402`/`403`).
3. Completed items in `allowed_projects` are written in a single off-thread SQLite transaction, and the new `sync_token` is persisted only after ingestion succeeds.
