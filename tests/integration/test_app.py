from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jmap_eas.config import AppConfig, ServerConfig


def _config(tmp_path) -> AppConfig:
    return AppConfig(server=ServerConfig(db_path=str(tmp_path / "bridge.sqlite3")))


def test_healthz_ok(tmp_path):
    from jmap_eas.app import create_app

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_lifespan_closes_registry_and_connection(tmp_path):
    from jmap_eas.app import create_app

    app = create_app(_config(tmp_path))
    with TestClient(app):
        pass
    # The database connection is closed on shutdown; further use must fail.
    import sqlite3

    try:
        app.state.jmap_eas.connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("expected the connection to be closed after shutdown")


def test_create_app_requires_config_env_var(monkeypatch):
    from jmap_eas.app import create_app

    monkeypatch.delenv("JMAP_EAS_CONFIG", raising=False)
    try:
        create_app()
    except RuntimeError as exc:
        assert "JMAP_EAS_CONFIG" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when JMAP_EAS_CONFIG is unset")


def test_main_requires_config_env_var(monkeypatch):
    from jmap_eas.app import main

    monkeypatch.delenv("JMAP_EAS_CONFIG", raising=False)
    with pytest.raises(SystemExit, match="JMAP_EAS_CONFIG"):
        main()


def test_main_starts_uvicorn_with_configured_host_and_port(tmp_path, monkeypatch):
    from jmap_eas import app as app_module

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
        [server]
        host = "127.0.0.1"
        port = 9999
        db_path = "{tmp_path / 'bridge.sqlite3'}"
        """
    )
    config_path.chmod(0o600)
    monkeypatch.setenv("JMAP_EAS_CONFIG", str(config_path))

    captured: dict[str, object] = {}

    def fake_run(app, *, host, port):
        captured["host"] = host
        captured["port"] = port
        app.state.jmap_eas.connection.close()

    monkeypatch.setattr(app_module.uvicorn, "run", fake_run)
    app_module.main()
    assert captured == {"host": "127.0.0.1", "port": 9999}
