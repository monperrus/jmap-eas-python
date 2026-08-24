"""Environment/file configuration for the JMAP-over-EAS bridge."""
from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class ConfigError(Exception):
    """The configuration file is missing, unreadable, or invalid."""


class AccountConfig(BaseModel):
    """One EAS mailbox exposed as a JMAP account.

    `api_token` (or `api_token_env`) is the credential JMAP clients present to
    *this bridge* over HTTP Basic auth -- distinct from the EAS password, so a
    bridge secret leak never exposes the mailbox password and vice versa.
    """

    eas_server: str
    username: str
    device_id: str
    password: str | None = None
    password_env: str | None = None
    api_token: str | None = None
    api_token_env: str | None = None
    user: str | None = None
    verify_ssl: bool = True
    timeout: float = 30.0

    @model_validator(mode="after")
    def _exactly_one_password_source(self) -> AccountConfig:
        if (self.password is None) == (self.password_env is None):
            raise ValueError("set exactly one of 'password' or 'password_env'")
        if (self.api_token is None) == (self.api_token_env is None):
            raise ValueError("set exactly one of 'api_token' or 'api_token_env'")
        return self

    def resolve_password(self) -> str:
        """Read the account password, either inline or from the configured environment variable."""
        return self._resolve_secret(self.password, self.password_env)

    def resolve_api_token(self) -> str:
        """Read the bridge-facing API token, either inline or from the configured environment variable."""
        return self._resolve_secret(self.api_token, self.api_token_env)

    @staticmethod
    def _resolve_secret(value: str | None, env_var: str | None) -> str:
        if value is not None:
            return value
        assert env_var is not None
        resolved = os.environ.get(env_var)
        if resolved is None:
            raise ConfigError(f"environment variable {env_var!r} is not set")
        return resolved


class PolicyConfig(BaseModel):
    """Deployment switches for operations that mutate the mailbox."""

    allow_moves: bool = True
    allow_delete: bool = True
    allow_send: bool = True


class ServerConfig(BaseModel):
    """HTTP listener and local storage settings."""

    host: str = "127.0.0.1"
    port: int = 8080
    db_path: str = "jmap-eas.sqlite3"


class AppConfig(BaseModel):
    """Top-level bridge configuration: listener, policy, and configured accounts."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    accounts: dict[str, AccountConfig] = Field(default_factory=dict)


def _check_permissions(path: Path) -> None:
    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} must be owner-readable only (found mode {oct(mode)}); run: chmod 600 {path}")


def load_config(path: str | Path) -> AppConfig:
    """Load and validate the bridge configuration from a TOML file.

    The file may contain account passwords and is therefore required to be
    owner-readable only, matching the operational policy in plan.md section 6.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    _check_permissions(config_path)
    try:
        data = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc
    return AppConfig.model_validate(data)
