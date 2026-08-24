"""ASGI application and lifecycle for the JMAP-over-EAS bridge."""
from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .config import AppConfig, load_config
from .registry import AccountRegistry
from .store import db as store_db

CONFIG_ENV_VAR = "JMAP_EAS_CONFIG"


class AppState:
    """Resources shared across requests: configuration, EAS registry, and the database."""

    def __init__(self, config: AppConfig, registry: AccountRegistry, connection: sqlite3.Connection) -> None:
        self.config = config
        self.registry = registry
        self.connection = connection


def _build_state(config: AppConfig) -> AppState:
    connection = store_db.connect(config.server.db_path)
    registry = AccountRegistry(config.accounts)
    return AppState(config, registry, connection)


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None]:
    state: AppState = app.state.jmap_eas
    try:
        yield
    finally:
        state.registry.close_all()
        state.connection.close()


async def healthz(request: Request) -> JSONResponse:
    """Liveness probe: process up and the local database reachable. No secrets, no EAS calls."""
    state: AppState = request.app.state.jmap_eas
    try:
        state.connection.execute("SELECT 1")
    except sqlite3.Error:
        return JSONResponse({"status": "error", "version": __version__}, status_code=503)
    return JSONResponse({"status": "ok", "version": __version__})


def create_app(config: AppConfig | None = None) -> Starlette:
    """Build the ASGI application. `config` defaults to loading the file named by `JMAP_EAS_CONFIG`."""
    if config is None:
        config_path = os.environ.get(CONFIG_ENV_VAR)
        if not config_path:
            raise RuntimeError(f"set {CONFIG_ENV_VAR} to the path of the bridge's TOML configuration file")
        config = load_config(config_path)
    app = Starlette(routes=[Route("/healthz", healthz)], lifespan=_lifespan)
    app.state.jmap_eas = _build_state(config)
    return app


def main() -> None:
    config_path = os.environ.get(CONFIG_ENV_VAR)
    if not config_path:
        raise SystemExit(f"set {CONFIG_ENV_VAR} to the path of the bridge's TOML configuration file")
    config = load_config(config_path)
    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
