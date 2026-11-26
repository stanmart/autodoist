# autodoist

A lean MCP server and mini-backend sitting between an MCP client (e.g. **Gemini**) and **Todoist** (API v1), with code-enforced guardrails, OIDC / OAuth 2.1 (`RemoteAuthProvider` + `JWTVerifier`), and SQLite completion history.

## MCP Tools (5)

- `get_tasks_by_synergy`: Fetch active tasks within allowed projects, grouped by synergy label and sorted by priority/due date.
- `capture_items`: Batch-create tasks (enforcing capture project allowlist, duplicate skipping, and mandatory `review` label) or log `post_hoc` completions directly to SQLite.
- `update_or_reschedule`: Safely update labels/priority/project or move due dates while preserving recurrence and keeping `review` unless `clear_review_flag=true`.
- `complete_task`: Close an allowed task in Todoist and immediately log it in `completion_log`.
- `get_completion_history`: Query up to 365 days of completion history from SQLite.

## Configuration

Non-secret settings live in [config.toml](config.toml) (`AUTODOIST_CONFIG` overrides the path):

- `[storage]`: `db_path` (default `data/autodoist.db`)
- `[auth]`: `mcp_base_url`, `oidc_issuer`, optional `oidc_jwks_uri`, `jwt_algorithm` (default `RS256`), optional `jwt_audience`, `allowed_user_subs`
- `[rules]`: `allowed_projects`, `capture_projects`, `allowed_labels`, `synergy_labels`, `review_label`

Secrets live in `.env` (see [.env.example](.env.example)):

- `TODOIST_API_TOKEN`
- `SYNC_SECRET`
- `AUTH_DISABLED=true` (local dev only — never use in production)

## Local Development

```bash
uv sync
cp .env.example .env
# Set TODOIST_API_TOKEN, SYNC_SECRET, and AUTH_DISABLED=true in .env for local testing
uv run uvicorn src.server:create_app --factory --reload --port 8000
```

Run tests, lint, and type-check:

```bash
uv run pytest
uv run ruff check .
uv run pyrefly check
```

## Docker Compose Deployment

1. Copy `.env.example` to `.env` and set `TODOIST_API_TOKEN` and `SYNC_SECRET`.
2. Edit `config.toml` with your `mcp_base_url`, `oidc_issuer` (plus `oidc_jwks_uri` if your provider does not use `/.well-known/jwks.json`), and `allowed_user_subs`.
3. Start behind an HTTPS reverse proxy (Caddy/Nginx/Traefik) pointing at `127.0.0.1:8000`:

```bash
docker compose up -d
```

4. Schedule periodic incremental syncs (every 30–60 minutes) with cron or a systemd timer:

```bash
curl -fsS -X POST https://your-domain.example/internal/sync \
  -H "X-Sync-Secret: $SYNC_SECRET"
```

5. In Gemini (**Customize -> Connected Apps -> + Custom -> Cloud MCP**), add `https://your-domain.example/mcp` and complete the OIDC login flow. Once verified, set `jwt_audience` in `config.toml` to the `aud` claim issued by your OIDC provider.
