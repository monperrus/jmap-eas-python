"""jmap-eas-python: a JMAP (RFC 8620/8621) bridge in front of an Exchange ActiveSync 16.1 mailbox."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jmap-eas-python")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
