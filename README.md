# autodoist

A lean MCP server and mini-backend sitting between an MCP client (e.g. **Gemini**) and **Todoist** (API v1), with code-enforced guardrails, OIDC / OAuth 2.1 (`RemoteAuthProvider` + `JWTVerifier`), and SQLite completion history.

## MCP Tools (6)

- `get_tasks_by_synergy`: Fetch active tasks within allowed projects, grouped by synergy label and sorted by priority/due date.
- `capture_items`: Batch-create tasks (enforcing capture project allowlist, duplicate skipping, and mandatory `review` label) or log `post_hoc` completions directly to SQLite (with retry dedup).
- `update_or_reschedule`: Rewrite a task's `content`/`notes`, update labels/priority/project, or change its schedule. `new_due_date` moves the date while preserving recurrence and the time of day; `due_string` replaces the whole schedule (e.g. `every 2 weeks on thu`, or `no date` to clear it). `review` is kept unless `clear_review_flag=true`. `labels` replaces only labels in `allowed_labels`; labels set by hand in Todoist are kept.
- `complete_task`: Close an allowed task in Todoist and immediately log it in `completion_log`. Completing an already completed task is a no-op.
- `drop_task`: Delete an allowed task that will not be done. A snapshot is kept in the `dropped_tasks` SQLite table, and nothing is added to the completion history. Tasks with subtasks are refused.
- `get_completion_history`: Query up to 365 days of completion history from SQLite.

## Configuration

Non-secret settings live in `config.toml` (copy from [config.example.toml](config.example.toml); `AUTODOIST_CONFIG` overrides the path):

- `[storage]`: `db_path` (default `data/autodoist.db`)
- `[auth]`: `mcp_base_url`, `oidc_issuer`, optional `oidc_jwks_uri`, `jwt_algorithm` (default `RS256`), `jwt_audience` (required when `AUTH_DISABLED=false`), `allowed_user_subs`
- `[sync]`: `interval_minutes` (default `30`; `0` disables background sync)
- `[rules]`: `allowed_projects`, `capture_projects`, `allowed_labels`, `synergy_labels`, `review_label`, `timezone` (default `UTC`)

Projects are allowlisted by name. Todoist lets several projects share a name, so if an allowlisted name matches more than one active project, every tool call that reads or writes Todoist tasks in it is rejected until one of them is renamed. Background sync skips it meanwhile, so completions made in that period are not added to the history.

Secrets live in `.env` (see [.env.example](.env.example)):

- `TODOIST_API_TOKEN`
- `AUTH_DISABLED=true` (local dev only — never use in production)

## Local Development

```bash
uv sync
cp config.example.toml config.toml
cp .env.example .env
# Set TODOIST_API_TOKEN and AUTH_DISABLED=true in .env for local testing
uv run uvicorn autodoist.server:create_app --factory --reload --port 8000
```

Trigger a one-off sync manually:

```bash
uv run python -m autodoist.sync_worker
```

Run tests, lint, and type-check:

```bash
uv run pytest
uv run ruff check .
uv run pyrefly check
```

## Docker Compose Deployment (VPS / Synology NAS)

The HTTP server runs in stateless JSON response mode (`stateless_http=True, json_response=True`) so it works cleanly behind Synology DSM's Nginx reverse proxy without SSE buffering issues or session drops across container restarts.

1. Copy `.env.example` to `.env` and set `TODOIST_API_TOKEN`.
2. Copy `config.example.toml` to `config.toml` and set `mcp_base_url`, `oidc_issuer` (plus `oidc_jwks_uri` if your provider does not use `/.well-known/jwks.json`), `jwt_audience`, `allowed_user_subs`, and `rules.timezone`.
3. Ensure `config.toml` is readable by the container user. If you use a Synology host bind mount instead of the named `autodoist_data` volume, either set `user: "<uid>:<gid>"` (e.g. `1026:100`) in `compose.yaml` or `chown -R 10001:10001 /path/to/data`.
4. Start behind an HTTPS reverse proxy pointing at `127.0.0.1:8000`:

```bash
docker compose up -d
```

Incremental sync runs automatically inside the container every `sync.interval_minutes` (default 30 min). To run a manual sync immediately:

```bash
docker exec autodoist python -m autodoist.sync_worker
```

5. In Gemini (**Customize -> Connected Apps -> + Custom -> Cloud MCP**), add `https://your-domain.example/mcp` and complete the OIDC login flow.
