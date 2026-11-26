from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class RulesConfig(BaseModel):
    """Project and label guardrails loaded from config.toml."""

    allowed_projects: list[str] = Field(min_length=1)
    capture_projects: list[str] = Field(min_length=1)
    allowed_labels: list[str] = Field(min_length=1)
    synergy_labels: list[str] = Field(default_factory=list)
    review_label: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_subsets(self) -> RulesConfig:
        allowed_proj_set = set(self.allowed_projects)
        invalid_capture = set(self.capture_projects) - allowed_proj_set
        if invalid_capture:
            invalid = sorted(invalid_capture)
            raise ValueError(
                f"capture_projects must be a subset of allowed_projects; invalid: {invalid}"
            )

        allowed_label_set = set(self.allowed_labels)
        invalid_synergy = set(self.synergy_labels) - allowed_label_set
        if invalid_synergy:
            invalid = sorted(invalid_synergy)
            raise ValueError(
                f"synergy_labels must be a subset of allowed_labels; invalid: {invalid}"
            )

        if self.review_label not in allowed_label_set:
            raise ValueError(
                f"review_label {self.review_label!r} must be present in allowed_labels"
            )
        return self

    @classmethod
    def from_toml(cls, path: str | Path) -> RulesConfig:
        config_path = Path(path)
        if not config_path.is_file():
            raise FileNotFoundError(f"Rules config file not found: {config_path}")
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)


class Settings(BaseSettings):
    """Runtime environment configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    todoist_api_token: str = ""
    sync_secret: str = ""
    mcp_base_url: str = ""
    pocket_id_issuer: str = "https://id.csigahaz.eu"
    jwt_audience: str | None = None
    allowed_user_subs: Annotated[frozenset[str], NoDecode] = frozenset()
    db_path: str = "data/autodoist.db"
    autodoist_config: str = "config.toml"
    auth_disabled: bool = False

    @field_validator("allowed_user_subs", mode="before")
    @classmethod
    def parse_allowed_user_subs(cls, value: object) -> frozenset[str]:
        if value is None or value == "":
            return frozenset()
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(str(part).strip() for part in value if str(part).strip())
        raise ValueError("allowed_user_subs must be a comma-separated string or collection")

    @field_validator("jwt_audience", mode="before")
    @classmethod
    def empty_str_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("pocket_id_issuer", "mcp_base_url", mode="after")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    def validate_runtime_security(self) -> None:
        """Fail closed if production auth settings are incomplete."""
        if self.auth_disabled:
            return
        if not self.mcp_base_url:
            raise ValueError("MCP_BASE_URL must be set when AUTH_DISABLED is false")
        if not self.pocket_id_issuer:
            raise ValueError("POCKET_ID_ISSUER must be set when AUTH_DISABLED is false")
        if not self.allowed_user_subs:
            raise ValueError(
                "ALLOWED_USER_SUBS must contain at least one sub when AUTH_DISABLED is false"
            )
