from __future__ import annotations

import os
import stat

import pytest
from pydantic import ValidationError

from jmap_eas.config import AccountConfig, ConfigError, load_config


def _write(path, content: str, mode: int = 0o600):
    path.write_text(content)
    path.chmod(mode)
    return path


def test_load_config_minimal(tmp_path):
    path = _write(
        tmp_path / "config.toml",
        """
        [accounts.alice]
        eas_server = "https://mail.example.com/Microsoft-Server-ActiveSync"
        username = "alice@example.com"
        password_env = "ALICE_PASSWORD"
        device_id = "dev1"
        """,
    )
    config = load_config(path)
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8080
    assert config.policy.allow_moves is True
    assert set(config.accounts) == {"alice"}
    assert config.accounts["alice"].username == "alice@example.com"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.toml")


def test_load_config_invalid_toml(tmp_path):
    path = _write(tmp_path / "config.toml", "not valid toml [[[")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


@pytest.mark.skipif(os.name != "posix", reason="permission bits are POSIX-only")
def test_load_config_rejects_group_readable_file(tmp_path):
    path = _write(tmp_path / "config.toml", "", mode=0o640)
    with pytest.raises(ConfigError, match="owner-readable only"):
        load_config(path)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_account_config_requires_exactly_one_password_source():
    base = dict(
        eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
        username="alice@example.com",
        device_id="dev1",
    )
    with pytest.raises(ValidationError):
        AccountConfig(**base)
    with pytest.raises(ValidationError):
        AccountConfig(**base, password="secret", password_env="ALICE_PASSWORD")


def test_account_config_resolve_password_inline():
    account = AccountConfig(
        eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
        username="alice@example.com",
        device_id="dev1",
        password="secret",
    )
    assert account.resolve_password() == "secret"


def test_account_config_resolve_password_from_env(monkeypatch):
    monkeypatch.setenv("ALICE_PASSWORD", "secret-from-env")
    account = AccountConfig(
        eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
        username="alice@example.com",
        device_id="dev1",
        password_env="ALICE_PASSWORD",
    )
    assert account.resolve_password() == "secret-from-env"


def test_account_config_resolve_password_missing_env(monkeypatch):
    monkeypatch.delenv("MISSING_PASSWORD", raising=False)
    account = AccountConfig(
        eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
        username="alice@example.com",
        device_id="dev1",
        password_env="MISSING_PASSWORD",
    )
    with pytest.raises(ConfigError, match="MISSING_PASSWORD"):
        account.resolve_password()
