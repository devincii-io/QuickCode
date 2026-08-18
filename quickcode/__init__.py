"""QuickCode — a local-first coding agent with a traceable web UI."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # The installed distribution is the single source of truth for the
    # version; a literal here would be a second one, and second sources of
    # truth drift.
    __version__ = _version("quickcode")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0-dev"
