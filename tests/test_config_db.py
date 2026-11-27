from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autodoist.config import (
    AppConfig,
    AuthConfig,
    EnvSettings,
    RulesConfig,
    SyncConfig,
    validate_runtime_security,
    validate_todoist_token,
)
from autodoist.db import CompletionRecord, Database, normalize_timestamp


def test_app_config_loads_default_toml() -> None:
    cfg = AppConfig.from_toml(Path("config.example.toml"))
    assert cfg.storage.db_path == "data/autodoist.db"
    assert cfg.auth.oidc_issuer == "https://id.example.com"
    assert cfg.auth.jwt_algorithm == "RS256"
    assert cfg.sync.interval_minutes == 30
    assert cfg.rules.allowed_projects == ["Inbox", "Personal", "Household"]
    assert cfg.rules.capture_projects == ["Personal", "Household"]
    assert cfg.rules.review_label is None
    assert cfg.rules.timezone == "UTC"


def test_app_config_rejects_invalid_subsets_and_unknown_keys(tmp_path: Path) -> None:
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text(
        "[rules]\n"
        'allowed_projects = ["Personal"]\n'
        'capture_projects = ["Inbox"]\n'
        'allowed_labels = ["errands"]\n'
        'synergy_labels = ["errands"]\n'
        'review_label = "review"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="capture_projects"):
        AppConfig.from_toml(bad_toml)

    typo_toml = tmp_path / "typo.toml"
    typo_toml.write_text(
        "[rules]\n"
        'allowed_projects = ["Personal"]\n'
        'capture_projects = ["Personal"]\n'
        'allowed_labels = ["review"]\n'
        'synergy_label = []\n'
        'review_label = "review"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        AppConfig.from_toml(typo_toml)


def test_runtime_security_fails_closed() -> None:
    rules = RulesConfig(
        allowed_projects=["Personal"],
        capture_projects=["Personal"],
        allowed_labels=["review"],
        synergy_labels=[],
        review_label="review",
    )
    cfg_no_subs = AppConfig(
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com/",
            oidc_issuer="https://id.example.com/",
            jwt_audience="https://mcp.example.com",
            allowed_user_subs=frozenset(),
        ),
        rules=rules,
    )
    assert cfg_no_subs.auth.mcp_base_url == "https://mcp.example.com"
    assert cfg_no_subs.auth.oidc_issuer == "https://id.example.com"

    prod_env = EnvSettings(_env_file=None, auth_disabled=False)
    with pytest.raises(ValueError, match="allowed_user_subs"):
        validate_runtime_security(cfg_no_subs, prod_env)

    cfg_no_aud = AppConfig(
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com",
            oidc_issuer="https://id.example.com",
            allowed_user_subs=["user-1"],
        ),
        rules=rules,
    )
    with pytest.raises(ValueError, match="jwt_audience"):
        validate_runtime_security(cfg_no_aud, prod_env)

    cfg_no_issuer = AppConfig(
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com",
            jwt_audience="https://mcp.example.com",
            allowed_user_subs=["user-1"],
        ),
        rules=rules,
    )
    with pytest.raises(ValueError, match="oidc_issuer"):
        validate_runtime_security(cfg_no_issuer, prod_env)

    cfg_ok = AppConfig(
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com",
            oidc_issuer="https://id.example.com",
            jwt_audience="https://mcp.example.com",
            allowed_user_subs=["user-1", " user-2 "],
        ),
        rules=rules,
    )
    assert cfg_ok.auth.allowed_user_subs == frozenset({"user-1", "user-2"})
    validate_runtime_security(cfg_ok, prod_env)

    with pytest.raises(ValueError):
        SyncConfig.model_validate_json('{"interval_minutes": -1}')

    dev_env = EnvSettings(_env_file=None, auth_disabled=True)
    validate_runtime_security(cfg_no_subs, dev_env)


def test_db_sync_state_and_completion_dedup(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "test.db")
    db.init_db()

    assert db.get_sync_state() == ("*", None)
    db.update_sync_state("tok-1", "2026-11-26T10:00:00Z")
    assert db.get_sync_state() == ("tok-1", "2026-11-26T10:00:00Z")

    now = datetime.now(UTC)
    assert (
        db.record_completion(
            todoist_task_id="123",
            content="Buy diapers",
            project_name="Household",
            labels=["errands", "review"],
            completed_at=now,
            source="todoist_sync",
        )
        is True
    )
    assert (
        db.record_completion(
            todoist_task_id="123",
            content="Buy diapers",
            project_name="Household",
            labels=["errands"],
            completed_at=now,
            source="todoist_sync",
        )
        is False
    )

    # Post-hoc logs with NULL task_id do not collide with each other
    assert (
        db.record_completion(
            todoist_task_id=None,
            content="Bought milk",
            project_name="Personal",
            completed_at=now,
            source="post_hoc_log",
        )
        is True
    )
    assert (
        db.record_completion(
            todoist_task_id=None,
            content="Bought bread",
            project_name="Personal",
            completed_at=now,
            source="post_hoc_log",
        )
        is True
    )
    # Old item outside window is excluded
    db.record_completion(
        todoist_task_id="old-1",
        content="Old task",
        project_name="Personal",
        completed_at=now - timedelta(days=120),
        source="mcp_complete",
    )

    personal = db.get_completion_history(days=90, project="Personal")
    assert {row.content for row in personal} == {"Bought milk", "Bought bread"}
    all_recent = db.get_completion_history(days=90)
    assert len(all_recent) == 3

    with pytest.raises(ValueError, match="between 1 and 365"):
        db.get_completion_history(days=0)
    with pytest.raises(ValueError, match="between 1 and 365"):
        db.get_completion_history(days=366)
    with pytest.raises(ValueError):
        db.record_completion(
            todoist_task_id="bad-ts",
            content="Empty ts",
            project_name="Personal",
            completed_at="",
            source="post_hoc_log",
        )


def test_db_reconciles_mcp_complete_and_checks_recent_post_hoc(tmp_path: Path) -> None:
    db = Database(tmp_path / "rec.db")
    db.init_db()
    db.ping()
    now = datetime.now(UTC)
    server_ts = now + timedelta(seconds=3)

    # Initial mcp_complete row + older recurring row, followed by authoritative todoist_sync
    inserted = db.record_completions(
        [
            CompletionRecord(
                todoist_task_id="rec-1",
                content="Water plants",
                project_name="Household",
                completed_at=now - timedelta(days=7),
                source="todoist_sync",
            ),
            CompletionRecord(
                todoist_task_id="rec-1",
                content="Water plants",
                project_name="Household",
                completed_at=now,
                source="mcp_complete",
            ),
            CompletionRecord(
                todoist_task_id="rec-1",
                content="Water plants",
                project_name="Household",
                completed_at=server_ts,
                source="todoist_sync",
            ),
        ]
    )
    assert inserted == 2
    history = db.get_completion_history(days=30)
    assert len(history) == 2
    # Reconciled row carries Todoist's authoritative server timestamp
    assert history[0].completed_at == normalize_timestamp(server_ts)

    # Distinct todoist_sync timestamps (without mcp_complete) are NOT suppressed by heuristic
    assert (
        db.record_completion(
            todoist_task_id="rec-2",
            content="Stretch",
            project_name="Personal",
            completed_at=now,
            source="todoist_sync",
        )
        is True
    )
    assert (
        db.record_completion(
            todoist_task_id="rec-2",
            content="Stretch",
            project_name="Personal",
            completed_at=now + timedelta(seconds=10),
            source="todoist_sync",
        )
        is True
    )

    # Same-second mcp_complete + todoist_sync preserves row and reconciles source
    assert (
        db.record_completion(
            todoist_task_id="rec-same-sec",
            content="Take pill",
            project_name="Personal",
            completed_at=now,
            source="mcp_complete",
        )
        is True
    )
    assert (
        db.record_completion(
            todoist_task_id="rec-same-sec",
            content="Take pill",
            project_name="Personal",
            completed_at=now,
            source="todoist_sync",
        )
        is False
    )
    assert [
        h.content for h in db.get_completion_history(days=30, project="Personal")
    ].count("Take pill") == 1

    # Separate todoist_sync row at ts + mcp_complete within +-60s deletes stale mcp_complete
    db.record_completion(
        todoist_task_id="rec-pre",
        content="Pre-existing sync",
        project_name="Personal",
        completed_at=now - timedelta(hours=1),
        source="todoist_sync",
    )
    db.record_completion(
        todoist_task_id="rec-pre",
        content="Pre-existing sync",
        project_name="Personal",
        completed_at=now - timedelta(hours=1, seconds=5),
        source="mcp_complete",
    )
    db.record_completion(
        todoist_task_id="rec-pre",
        content="Pre-existing sync",
        project_name="Personal",
        completed_at=now - timedelta(hours=1),
        source="todoist_sync",
    )
    assert [
        h.content for h in db.get_completion_history(days=30, project="Personal")
    ].count("Pre-existing sync") == 1

    # Post-hoc recent duplicate lookup normalizes case/whitespace and respects window
    db.record_completion(
        todoist_task_id=None,
        content="Bought diapers",
        project_name="Household",
        completed_at=now - timedelta(seconds=30),
        source="post_hoc_log",
    )
    db.record_completion(
        todoist_task_id=None,
        content="Old milk",
        project_name="Household",
        completed_at=now - timedelta(minutes=10),
        source="post_hoc_log",
    )
    assert db.has_recent_post_hoc("  bought   DIAPERS ", "Household", within_seconds=300) is True
    assert db.has_recent_post_hoc("bought diapers", "Personal", within_seconds=300) is False
    assert db.has_recent_post_hoc("old milk", "Household", within_seconds=300) is False


def test_auth_audience_list_jwks_and_algorithm_validation() -> None:
    auth = AuthConfig(
        jwt_audience=[" https://mcp.example.com ", "", "client-id"],
        oidc_jwks_uri=" https://id.example.com/oauth2/v1/keys ",
        jwt_algorithm="ES256",
    )
    assert auth.jwt_audience == ["https://mcp.example.com", "client-id"]
    assert auth.oidc_jwks_uri == "https://id.example.com/oauth2/v1/keys"
    assert auth.jwt_algorithm == "ES256"
    assert AuthConfig(jwt_audience=["  "], oidc_jwks_uri="  ").jwt_audience is None
    assert AuthConfig(oidc_jwks_uri="  ").oidc_jwks_uri is None

    for bad_alg in ("none", "HS256", " "):
        with pytest.raises(ValueError, match="jwt_algorithm"):
            AuthConfig(jwt_algorithm=bad_alg)

    with pytest.raises(ValueError, match="review_label"):
        RulesConfig(
            allowed_projects=["Personal"],
            capture_projects=["Personal"],
            allowed_labels=["errands"],
            synergy_labels=["errands"],
            review_label="review",
        )

    with pytest.raises(ValueError, match="Invalid IANA timezone"):
        RulesConfig(
            allowed_projects=["Personal"],
            capture_projects=["Personal"],
            allowed_labels=["review"],
            synergy_labels=[],
            review_label="review",
            timezone="Mars/Olympus",
        )

    with pytest.raises(ValueError, match="TODOIST_API_TOKEN"):
        validate_todoist_token(EnvSettings(_env_file=None, todoist_api_token="  "))
    validate_todoist_token(EnvSettings(_env_file=None, todoist_api_token="tok-ok"))
