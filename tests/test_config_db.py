from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.config import AppConfig, AuthConfig, EnvSettings, RulesConfig, validate_runtime_security
from src.db import Database


def test_app_config_loads_default_toml() -> None:
    cfg = AppConfig.from_toml(Path("config.toml"))
    assert cfg.storage.db_path == "data/autodoist.db"
    assert cfg.auth.pocket_id_issuer == "https://id.csigahaz.eu"
    assert cfg.rules.allowed_projects == ["Inbox", "Personal", "Household"]
    assert cfg.rules.capture_projects == ["Personal", "Household"]
    assert cfg.rules.review_label == "review"


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
        auth=AuthConfig(mcp_base_url="https://mcp.example.com/", allowed_user_subs=frozenset()),
        rules=rules,
    )
    assert cfg_no_subs.auth.mcp_base_url == "https://mcp.example.com"

    prod_env = EnvSettings(_env_file=None, sync_secret="sync-ok", auth_disabled=False)
    with pytest.raises(ValueError, match="allowed_user_subs"):
        validate_runtime_security(cfg_no_subs, prod_env)

    cfg_ok = AppConfig(
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com",
            allowed_user_subs=["user-1", " user-2 "],
        ),
        rules=rules,
    )
    assert cfg_ok.auth.allowed_user_subs == frozenset({"user-1", "user-2"})
    validate_runtime_security(cfg_ok, prod_env)

    no_secret_env = EnvSettings(_env_file=None, sync_secret="", auth_disabled=False)
    with pytest.raises(ValueError, match="SYNC_SECRET"):
        validate_runtime_security(cfg_ok, no_secret_env)

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
    assert {row["content"] for row in personal} == {"Bought milk", "Bought bread"}
    all_recent = db.get_completion_history(days=90)
    assert len(all_recent) == 3

    with pytest.raises(ValueError, match="between 1 and 365"):
        db.get_completion_history(days=0)
