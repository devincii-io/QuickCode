"""QuickCode — a local-first coding agent with a traceable web UI."""

__all__ = ["__version__"]


def __getattr__(name: str) -> str:
    """Resolve ``__version__`` on first access, not on import.

    The installed distribution is the single source of truth for the version;
    a literal here would be a second one, and second sources of truth drift.
    But reading it costs an `importlib.metadata` import — ~140 ms of the ~340 ms
    it took to import anything under `quickcode.`, paid by every process
    whether or not it ever asks. Only `update.py` does. PEP 562 keeps the
    attribute working and moves the cost to the one caller.
    """
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("quickcode")
    except PackageNotFoundError:  # running from a source checkout
        return "0.0.0-dev"
