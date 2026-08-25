"""Method-call batching and RFC 8620 result-reference resolution (plan.md section 5).

Invocations execute in request order. Before each one, every `#`-suffixed
argument is resolved against an earlier successful response using the JSON
Pointer rules of RFC 6901 (`~0`/`~1` unescaping, array indices), plus JMAP's
`*` wildcard for mapping over an array (RFC 8620 section 3.7). A malformed or
unresolved reference becomes that call's `invalidResultReference` error and
does not abort the rest of the batch.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..backend.eas import EasAdapter
from ..backend.sync import SyncCoordinator
from ..config import PolicyConfig
from ..errors import InvalidResultReferenceError, JmapError, UnknownMethodError
from ..store.db import Database

Invocation = tuple[str, dict[str, Any], str]


@dataclass
class Environment:
    """Per-request context handed to every method handler."""

    account_id: str
    database: Database
    sync: SyncCoordinator
    adapter: EasAdapter
    policy: PolicyConfig
    identity_email: str = ""


MethodHandler = Callable[[Environment, dict[str, Any]], dict[str, Any]]


@dataclass
class _CallResult:
    name: str
    value: dict[str, Any]
    is_error: bool


def _unescape_pointer_segment(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def resolve_pointer(value: Any, path: str) -> Any:
    """Resolve an extended JSON Pointer: RFC 6901 plus JMAP's `*` array-mapping wildcard."""
    if path == "":
        return value
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/': {path!r}")
    return _resolve_segments(value, path[1:].split("/"))


def _resolve_segments(value: Any, segments: list[str]) -> Any:
    if not segments:
        return value
    head, *rest = segments
    if head == "*":
        if not isinstance(value, list):
            raise ValueError("'*' requires an array")
        collected: list[Any] = []
        for item in value:
            resolved = _resolve_segments(item, rest)
            if isinstance(resolved, list):
                collected.extend(resolved)
            else:
                collected.append(resolved)
        return collected
    key = _unescape_pointer_segment(head)
    if isinstance(value, list):
        try:
            index = int(key)
        except ValueError:
            raise ValueError(f"invalid array index: {key!r}") from None
        if not 0 <= index < len(value):
            raise ValueError(f"array index out of range: {index}")
        return _resolve_segments(value[index], rest)
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"no such property: {key!r}")
        return _resolve_segments(value[key], rest)
    raise ValueError("cannot traverse into a scalar value")


def _resolve_arguments(arguments: dict[str, Any], results: dict[str, _CallResult]) -> dict[str, Any]:
    resolved = dict(arguments)
    for key in list(resolved):
        if not key.endswith("#"):
            continue
        ref = resolved.pop(key)
        target_key = key[:-1]
        if not isinstance(ref, dict) or "resultOf" not in ref or "name" not in ref or "path" not in ref:
            raise InvalidResultReferenceError(f"malformed result reference for {target_key!r}")
        target = results.get(ref["resultOf"])
        if target is None or target.is_error or target.name != ref["name"]:
            raise InvalidResultReferenceError(f"no result to resolve {target_key!r} against")
        try:
            resolved[target_key] = resolve_pointer(target.value, ref["path"])
        except ValueError as exc:
            raise InvalidResultReferenceError(str(exc)) from exc
    return resolved


class Dispatcher:
    def __init__(self, methods: dict[str, MethodHandler]) -> None:
        self._methods = methods

    def execute(self, calls: list[Invocation], env: Environment) -> list[Invocation]:
        results: dict[str, _CallResult] = {}
        responses: list[Invocation] = []
        for name, arguments, call_id in calls:
            response_name, value, is_error = self._execute_one(name, arguments, results, env)
            results[call_id] = _CallResult(name=name, value=value, is_error=is_error)
            responses.append((response_name, value, call_id))
        return responses

    def _execute_one(
        self, name: str, arguments: dict[str, Any], results: dict[str, _CallResult], env: Environment
    ) -> tuple[str, dict[str, Any], bool]:
        try:
            resolved_arguments = _resolve_arguments(arguments, results)
        except JmapError as exc:
            return "error", {"type": exc.type}, True

        handler = self._methods.get(name)
        if handler is None:
            return "error", {"type": UnknownMethodError.type}, True

        try:
            result = handler(env, resolved_arguments)
        except JmapError as exc:
            return "error", {"type": exc.type}, True
        return name, result, False
