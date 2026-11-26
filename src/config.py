from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: str = "data/autodoist.db"

    @field_validator("db_path", mode="after")
    @classmethod
    def validate_db_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("db_path must be non-empty")
        return stripped


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mcp_base_url: str = ""
    oidc_issuer: str = ""
    oidc_jwks_uri: str | None = None
    jwt_algorithm: str = "RS256"
    jwt_audience: str | list[str] | None = None
    allowed_user_subs: frozenset[str] = frozenset()

    @field_validator("mcp_base_url", "oidc_issuer", mode="after")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("oidc_jwks_uri", mode="before")
    @classmethod
    def normalize_jwks_uri(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("jwt_algorithm", mode="after")
    @classmethod
    def validate_jwt_algorithm(cls, value: str) -> str:
        alg = value.strip()
        if not alg or alg.lower() == "none" or alg.upper().startswith("HS"):
            raise ValueError(f"Unsupported asymmetric jwt_algorithm: {value!r}")
        return alg

    @field_validator("jwt_audience", mode="before")
    @classmethod
    def normalize_jwt_audience(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            return cleaned or None
        return value

    @field_validator("allowed_user_subs", mode="before")
    @classmethod
    def parse_allowed_user_subs(cls, value: object) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(str(part).strip() for part in value if str(part).strip())
        raise ValueError("allowed_user_subs must be a list of user sub strings")


class RulesConfig(BaseModel):
    """Project and label guardrails loaded from config.toml."""

    model_config = ConfigDict(extra="forbid")

    allowed_projects: list[str] = Field(min_length=1)
    capture_projects: list[str] = Field(min_length=1)
    allowed_labels: list[str] = Field(min_length=1)
    synergy_labels: list[str] = Field(default_factory=list)
    review_label: str = Field(min_length=1)

    @field_validator(
        "allowed_projects",
        "capture_projects",
        "allowed_labels",
        "synergy_labels",
        mode="before",
    )
    @classmethod
    def strip_and_validate_items(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        cleaned = [str(item).strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("list items must be non-empty strings")
        return cleaned

    @field_validator("review_label", mode="before")
    @classmethod
    def strip_review_label(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("review_label must be a non-empty string")
            return stripped
        return value

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


class AppConfig(BaseModel):
    """All non-secret configuration loaded from config.toml."""

    model_config = ConfigDict(extra="forbid")

    storage: StorageConfig = Field(default_factory=StorageConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rules: RulesConfig

    @classmethod
    def from_toml(cls, path: str | Path) -> AppConfig:
        config_path = Path(path)
        if not config_path.is_file():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)


class EnvSettings(BaseSettings):
    """Secrets and minimal runtime toggles loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    todoist_api_token: str = ""
    sync_secret: str = ""
    autodoist_config: str = "config.toml"
    auth_disabled: bool = False


def validate_runtime_security(app_config: AppConfig, env: EnvSettings) -> None:
    """Fail closed if production auth or sync settings are incomplete."""
    if env.auth_disabled:
        return
    if not app_config.auth.mcp_base_url:
        raise ValueError("auth.mcp_base_url must be set when AUTH_DISABLED is false")
    if not app_config.auth.oidc_issuer:
        raise ValueError("auth.oidc_issuer must be set when AUTH_DISABLED is false")
    if not app_config.auth.allowed_user_subs:
        raise ValueError(
            "auth.allowed_user_subs must contain at least one sub when AUTH_DISABLED is false"
        )
    if not env.sync_secret.strip():
        raise ValueError("SYNC_SECRET must be set when AUTH_DISABLED is false")


def load_config() -> tuple[AppConfig, EnvSettings]:
    env = EnvSettings()
    app_config = AppConfig.from_toml(env.autodoist_config)
    return app_config, env
