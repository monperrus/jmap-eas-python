"""ASGI application and lifecycle for the JMAP-over-EAS bridge."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, policy
from .auth import AuthenticationError, authenticate
from .backend.sync import SyncCoordinator
from .config import AppConfig, load_config
from .errors import JmapError
from .jmap import blob as jmap_blob
from .jmap import eventsource as jmap_eventsource
from .jmap import session as jmap_session
from .jmap.dispatcher import Dispatcher, Environment, Invocation
from .jmap.sync_scope import compute_sync_scope
from .observability import Metrics, configure_logging, get_logger
from .registry import AccountContext, AccountRegistry
from .store import blobs as store_blobs
from .store import cache as store_cache
from .store import db as store_db
from .store import state as store_state
from .store.db import Database

EVENTSOURCE_IDLE_POLL_SECONDS = 5
EVENTSOURCE_ERROR_BACKOFF_SECONDS = 5

CONFIG_ENV_VAR = "JMAP_EAS_CONFIG"

_logger = get_logger("app")


class AppState:
    """Resources shared across requests: configuration, EAS registry, database, and dispatcher."""

    def __init__(
        self, config: AppConfig, registry: AccountRegistry, database: Database, sync: SyncCoordinator,
        dispatcher: Dispatcher, metrics: Metrics,
    ) -> None:
        self.config = config
        self.registry = registry
        self.database = database
        self.sync = sync
        self.dispatcher = dispatcher
        self.metrics = metrics


def _build_state(config: AppConfig) -> AppState:
    configure_logging()
    database = store_db.connect(config.server.db_path)
    registry = AccountRegistry(config.accounts)
    sync = SyncCoordinator(database)
    dispatcher = Dispatcher(policy.METHODS)
    return AppState(config, registry, database, sync, dispatcher, Metrics())


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
    return JSONResponse({"status": "ok", "version": __version__, "metrics": state.metrics.snapshot()})


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


def _check_using(payload: Any) -> str | None:
    """RFC 8620 section 3.1: validates the request's `using` capability list.

    Returns an error `type` if the request must be rejected, else `None`.
    Does not check that each *called method* has its capability listed --
    only that every listed capability is one this deployment recognizes and
    that the mandatory core capability is present.
    """
    using = payload.get("using") if isinstance(payload, dict) else None
    if not isinstance(using, list) or not all(isinstance(item, str) for item in using):
        return "notRequest"
    if policy.CORE_CAPABILITY not in using:
        return "notRequest"
    if any(capability not in policy.CAPABILITIES for capability in using):
        return "unknownCapability"
    return None


def _ensure_synced(state: AppState, context: AccountContext, account_id: str, calls: list[Invocation]) -> None:
    """Brings the cache only as fresh as this batch's calls actually need (plan.md section 1):
    e.g. `Identity/get` needs no EAS round trip at all, and an `Email/query` scoped by a single
    `inMailbox` needs only that mailbox's items, not every mailbox in the account. Each piece of
    work is further skipped if a recent-enough request already did it (`SyncCoordinator`'s
    request-scoped freshness window)."""
    scope = compute_sync_scope(calls)
    try:
        if scope.reconcile_folders:
            state.sync.ensure_folders_reconciled(account_id, context.command)
        folder_ids = set(scope.folder_ids)
        if scope.sync_all_folders:
            with state.database.transaction() as conn:
                folder_ids |= {m.mailbox_id for m in store_cache.list_mailboxes(conn, account_id)}
        for folder_id in folder_ids:
            state.sync.ensure_folder_synced(account_id, folder_id, context.command)
    except JmapError as exc:
        # Best-effort freshness; still serve whatever the cache already has. `exc` is
        # already redacted (BackendError never carries raw backend text) so it's safe to log.
        state.metrics.increment("sync_failures_total")
        _logger.warning("sync failed, serving from cache", extra={"fields": {
            "account_id": account_id, "error_type": exc.type,
        }})
    with state.database.transaction() as conn:
        store_state.prune_change_log(conn, account_id)


def _sync_and_dispatch(
    state: AppState, context: AccountContext, account_id: str, calls: list[Invocation]
) -> list[Invocation]:
    """Runs off the event loop (plan.md section 1): EAS calls are synchronous `requests`."""
    with context.command_lock:
        _ensure_synced(state, context, account_id, calls)
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
    using_error = _check_using(payload)
    if using_error is not None:
        return JSONResponse({"type": using_error}, status_code=400)
    calls = _parse_method_calls(payload)
    if calls is None:
        return JSONResponse({"type": "notRequest"}, status_code=400)

    state.metrics.increment("requests_total")
    started = time.monotonic()
    context = state.registry.get(account_id)
    responses = await run_in_threadpool(_sync_and_dispatch, state, context, account_id, calls)
    error_count = sum(1 for name, _value, _call_id in responses if name == "error")
    if error_count:
        state.metrics.increment("errors_total")
    _logger.info("api request completed", extra={"fields": {
        "account_id": account_id,
        "methods": [name for name, _args, _call_id in calls],
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "error_count": error_count,
    }})
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


async def _eventsource_stream(
    request: Request, state: AppState, context: AccountContext, account_id: str,
    params: jmap_eventsource.EventSourceParams,
) -> AsyncGenerator[str]:
    last_states = await run_in_threadpool(jmap_eventsource.current_states, state.database, account_id, params.types)
    yield jmap_eventsource.format_state_event(account_id, last_states)
    if params.close_after_state:
        return

    last_ping_sent = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        try:
            folder_ids = await run_in_threadpool(jmap_eventsource.mail_folder_ids, state.database, account_id)
            if not folder_ids:
                await asyncio.sleep(EVENTSOURCE_IDLE_POLL_SECONDS)
            else:
                # Blocks the worker thread for up to ~heartbeat seconds (a real EAS long poll);
                # a client disconnect during that window is only noticed on the next iteration.
                changed_folders = await run_in_threadpool(
                    jmap_eventsource.ping_and_sync, state.sync, context, account_id, folder_ids
                )
                if changed_folders:
                    new_states = await run_in_threadpool(
                        jmap_eventsource.current_states, state.database, account_id, params.types
                    )
                    delta = {t: v for t, v in new_states.items() if last_states.get(t) != v}
                    if delta:
                        last_states.update(delta)
                        yield jmap_eventsource.format_state_event(account_id, delta)
                        if params.close_after_state:
                            return
        except JmapError as exc:
            _logger.warning("eventsource ping/sync failed", extra={"fields": {
                "account_id": account_id, "error_type": exc.type,
            }})
            await asyncio.sleep(EVENTSOURCE_ERROR_BACKOFF_SECONDS)

        if params.ping_interval and time.monotonic() - last_ping_sent >= params.ping_interval:
            yield jmap_eventsource.format_ping_event(params.ping_interval)
            last_ping_sent = time.monotonic()


async def eventsource(request: Request) -> Response:
    """RFC 8620 section 7.3: `GET /eventsource`, an SSE push stream backed by EAS `Ping`."""
    state: AppState = request.app.state.jmap_eas
    account_id = _authenticate(request)
    if account_id is None:
        return _unauthorized()
    params = jmap_eventsource.parse_params(dict(request.query_params))
    context = state.registry.get(account_id)
    return StreamingResponse(
        _eventsource_stream(request, state, context, account_id, params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        Route("/eventsource", eventsource),
    ]
    middleware = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)
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
