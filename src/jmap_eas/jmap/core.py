"""`Core/echo` (RFC 8620 section 4.4)."""
from __future__ import annotations

from typing import Any

from .dispatcher import Environment


def echo(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    """Returns exactly what it was given."""
    return dict(arguments)
