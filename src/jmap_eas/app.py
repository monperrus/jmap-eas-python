"""ASGI application and lifecycle for the JMAP-over-EAS bridge."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__, policy
from .auth import AuthenticationError, authenticate
from .backend.sync import SyncCoordinator
from .config import AppConfig, load_config
from .errors import JmapError
from .jmap import blob as jmap_blob
from .jmap import session as jmap_session
from .jmap.dispatcher import Dispatcher, Environment, Invocation
from .registry import AccountContext, AccountRegistry
from .store import blobs as store_blobs
from .store import db as store_db
from .store.db import Database

CONFIG_ENV_VAR = "JMAP_EAS_CONFIG"


class AppState:
    """Resources shared across requests: configuration, EAS registry, database, and dispatcher."""

    def __init__(
        self, config: AppConfig, registry: AccountRegistry, database: Database, sync: SyncCoordinator,
        dispatcher: Dispatcher,
    ) -> None:
        self.config = config
        self.registry = registry
        self.database = database
        self.sync = sync
        self.dispatcher = dispatcher


def _build_state(config: AppConfig) -> AppState:
    database = store_db.connect(config.server.db_path)
    registry = AccountRegistry(config.accounts)
    sync = SyncCoordinator(database)
    dispatcher = Dispatcher(policy.METHODS)
    return AppState(config, registry, database, sync, dispatcher)


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None]:
    state: AppState = app.state.jmap_eas
    try:
        yield
    finally:
        state.registry.close_all()
        state.database.close()


def _authenticate(request: Request) -> str | None:
    state: AppState = request.app.state.jmap_eas
    try:
        return authenticate(request.headers.get("authorization"), state.config.accounts)
    except AuthenticationError:
        return None


def _unauthorized() -> JSONResponse:
    return JSONResponse({"type": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Basic"})


async def healthz(request: Request) -> JSONResponse:
    """Liveness probe: process up and the local database reachable. No secrets, no EAS calls."""
    state: AppState = request.app.state.jmap_eas
    try:
        state.database.execute("SELECT 1")
    except sqlite3.Error:
        return JSONResponse({"status": "error", "version": __version__}, status_code=503)
    return JSONResponse({"status": "ok", "version": __version__})


async def well_known_jmap(request: Request) -> Response:
    """RFC 8620 section 2: the JMAP session resource."""
    state: AppState = request.app.state.jmap_eas
    account_id = _authenticate(request)
    if account_id is None:
        return _unauthorized()
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(jmap_session.build_session(account_id, base_url, state.config.policy))


def _parse_method_calls(payload: Any) -> list[Invocation] | None:
    if not isinstance(payload, dict):
        return None
    calls = payload.get("methodCalls")
    if not isinstance(calls, list):
        return None
    parsed: list[Invocation] = []
    for entry in calls:
        if not (isinstance(entry, list) and len(entry) == 3 and isinstance(entry[0], str)
                and isinstance(entry[1], dict) and isinstance(entry[2], str)):
            return None
        parsed.append((entry[0], entry[1], entry[2]))
    return parsed


def _sync_and_dispatch(
    state: AppState, context: AccountContext, account_id: str, calls: list[Invocation]
) -> list[Invocation]:
    """Runs off the event loop (plan.md section 1): EAS calls are synchronous `requests`."""
    with context.command_lock:
        try:
            state.sync.sync_account(account_id, context.command)
        except JmapError:
            pass  # best-effort freshness; still serve whatever the cache already has
        account_config = state.config.accounts[account_id]
        env = Environment(
            account_id=account_id, database=state.database, sync=state.sync, adapter=context.command,
            policy=state.config.policy, identity_email=account_config.user or account_config.username,
        )
        return state.dispatcher.execute(calls, env)


async def api(request: Request) -> Response:
    """RFC 8620 section 3.3: `POST /api`, one batch of method calls."""
    state: AppState = request.app.state.jmap_eas
    account_id = _authenticate(request)
    if account_id is None:
        return _unauthorized()

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"type": "notJSON"}, status_code=400)
    calls = _parse_method_calls(payload)
    if calls is None:
        return JSONResponse({"type": "notRequest"}, status_code=400)

    context = state.registry.get(account_id)
    responses = await run_in_threadpool(_sync_and_dispatch, state, context, account_id, calls)
    return JSONResponse({
        "methodResponses": [list(response) for response in responses],
        "sessionState": "single-account",
    })


def _resolve_blob(
    state: AppState, context: AccountContext, account_id: str, locator: jmap_blob.BlobLocator
) -> tuple[bytes, str] | None:
    with context.command_lock:
        return jmap_blob.resolve_blob(locator, account_id=account_id, adapter=context.command,
                                       database=state.database)


async def download(request: Request) -> Response:
    """`GET /download/{accountId}/{blobId}/{name}` (the session resource's `downloadUrl` template)."""
    state: AppState = request.app.state.jmap_eas
    account_id = _authenticate(request)
    if account_id is None:
        return _unauthorized()
    if request.path_params["account_id"] != account_id:
        return JSONResponse({"type": "accountNotFound"}, status_code=404)

    try:
        locator = jmap_blob.decode_blob_id(request.path_params["blob_id"])
    except ValueError:
        return JSONResponse({"type": "notFound"}, status_code=404)

    context = state.registry.get(account_id)
    resolved = await run_in_threadpool(_resolve_blob, state, context, account_id, locator)
    if resolved is None:
        return JSONResponse({"type": "notFound"}, status_code=404)
    data, content_type = resolved
    return Response(data, media_type=content_type)


def _store_upload(state: AppState, account_id: str, upload_id: str, content_type: str, data: bytes) -> None:
    with state.database.transaction() as conn:
        store_blobs.insert_blob(conn, account_id, upload_id, content_type, data)


async def upload(request: Request) -> Response:
    """RFC 8620 section 6.1: `POST /upload/{accountId}`."""
    state: AppState = request.app.state.jmap_eas
    account_id = _authenticate(request)
    if account_id is None:
        return _unauthorized()
    if request.path_params["account_id"] != account_id:
        return JSONResponse({"type": "accountNotFound"}, status_code=404)

    data = await request.body()
    if len(data) > policy.MAX_UPLOAD_SIZE_BYTES:
        return JSONResponse({"type": "requestTooLarge"}, status_code=413)
    content_type = request.headers.get("content-type") or "application/octet-stream"

    upload_id = secrets.token_urlsafe(16)
    await run_in_threadpool(_store_upload, state, account_id, upload_id, content_type, data)
    return JSONResponse({
        "accountId": account_id,
        "blobId": jmap_blob.encode_upload_blob_id(upload_id),
        "type": content_type,
        "size": len(data),
    })


def create_app(config: AppConfig | None = None) -> Starlette:
    """Build the ASGI application. `config` defaults to loading the file named by `JMAP_EAS_CONFIG`."""
    if config is None:
        config_path = os.environ.get(CONFIG_ENV_VAR)
        if not config_path:
            raise RuntimeError(f"set {CONFIG_ENV_VAR} to the path of the bridge's TOML configuration file")
        config = load_config(config_path)
    routes = [
        Route("/healthz", healthz),
        Route("/.well-known/jmap", well_known_jmap),
        Route("/api", api, methods=["POST"]),
        Route("/download/{account_id}/{blob_id}/{name}", download),
        Route("/upload/{account_id}", upload, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=_lifespan)
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
