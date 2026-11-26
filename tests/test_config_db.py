from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.config import RulesConfig, Settings
from src.db import Database


def test_rules_config_loads_default_toml() -> None:
    rules = RulesConfig.from_toml(Path("config.toml"))
    assert rules.allowed_projects == ["Inbox", "Personal", "Household"]
    assert rules.capture_projects == ["Personal", "Household"]
    assert rules.review_label == "review"


def test_rules_config_rejects_invalid_subsets(tmp_path: Path) -> None:
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text(
        'allowed_projects = ["Personal"]\n'
        'capture_projects = ["Inbox"]\n'
        'allowed_labels = ["errands"]\n'
        'synergy_labels = ["errands"]\n'
        'review_label = "review"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="capture_projects"):
        RulesConfig.from_toml(bad_toml)


def test_settings_fail_closed_when_auth_enabled() -> None:
    settings = Settings(
        _env_file=None,
        mcp_base_url="https://mcp.example.com/",
        allowed_user_subs="",
        auth_disabled=False,
    )
    assert settings.mcp_base_url == "https://mcp.example.com"
    with pytest.raises(ValueError, match="ALLOWED_USER_SUBS"):
        settings.validate_runtime_security()

    dev_settings = Settings(_env_file=None, auth_disabled=True)
    dev_settings.validate_runtime_security()

    prod_settings = Settings(
        _env_file=None,
        mcp_base_url="https://mcp.example.com",
        allowed_user_subs="user-1, user-2",
        auth_disabled=False,
    )
    assert prod_settings.allowed_user_subs == frozenset({"user-1", "user-2"})
    prod_settings.validate_runtime_security()


def test_db_sync_state_and_completion_dedup(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "test.db")
    db.init_db()

    assert db.get_sync_state() == ("*", None)
    db.update_sync_state("tok-1", "2026-11-26T10:00:00Z")
    assert db.get_sync_state() == ("tok-1", "2026-11-26T10:00:00Z")

    now = datetime.now(UTC)
    inserted_first = db.record_completion(
        todoist_task_id="123",
        content="Buy diapers",
        project_name="Household",
        labels=["errands", "review"],
        completed_at=now,
        source="todoist_sync",
    )
    inserted_dup = db.record_completion(
        todoist_task_id="123",
        content="Buy diapers",
        project_name="Household",
        labels=["errands"],
        completed_at=now,
        source="todoist_sync",
    )
    assert inserted_first is True
    assert inserted_dup is False

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
