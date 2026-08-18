"""Encrypted at-rest storage for API keys.

A key may be provided two ways, checked in this order:
  1. an environment variable (fixed name per key);
  2. a value saved from Settings, encrypted on disk.

On Windows the on-disk value is protected with DPAPI (CryptProtectData, tied to
the current user account) via ctypes — no third-party dependency. On other
platforms we fall back to a 0600 file with a light obfuscation (DPAPI has no
POSIX equivalent without extra deps; the file-permission is the real control).

The OpenRouter key was the first tenant and keeps its own four functions and
its historical path, ``~/.quickcode/openrouter.key``. Everything else — the
web-search provider keys — goes through the named API below, into
``~/.quickcode/<name>.key`` beside it. One store, one encryption path, one
place to look when revoking: a second secret mechanism is how a key ends up
somewhere nobody remembers to clear.
"""

from __future__ import annotations

import base64
import os
import platform
import re
from pathlib import Path

API_KEY_ENV = "QUICKCODE_OPENROUTER_API_KEY"

SECRETS_DIR = Path.home() / ".quickcode"
_SECRET_PATH = SECRETS_DIR / "openrouter.key"
_IS_WINDOWS = platform.system() == "Windows"

# A secret name becomes a filename, so it may not become a path.
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


# --------------------------------------------------------------------------- #
# Windows DPAPI via ctypes
# --------------------------------------------------------------------------- #
def _dpapi(protect: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(raw: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    blob_in = to_blob(data)
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI operation failed")
    size = blob_out.cbData
    out = ctypes.string_at(blob_out.pbData, size)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value.encode("utf-8")
    if _IS_WINDOWS:
        payload = b"DPAPI:" + _dpapi(True, raw)
    else:
        # Not real encryption — the 0600 file permission is the control.
        payload = b"B64:" + base64.b64encode(raw)
    path.write_bytes(payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_secret(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = path.read_bytes()
        if payload.startswith(b"DPAPI:"):
            return _dpapi(False, payload[6:]).decode("utf-8")
        if payload.startswith(b"B64:"):
            return base64.b64decode(payload[4:]).decode("utf-8")
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# Named secrets (web-search provider keys)
# --------------------------------------------------------------------------- #
def secret_path(name: str) -> Path:
    """Where a named secret lives. Rejects anything that is not a plain name."""
    if not _SAFE_NAME.fullmatch(name or ""):
        raise ValueError(
            f"invalid secret name {name!r}: lowercase letters, digits, '-' and '_' only"
        )
    return SECRETS_DIR / f"{name}.key"


def save_secret(name: str, value: str) -> None:
    _write_secret(secret_path(name), value)


def load_secret(name: str) -> str | None:
    try:
        return _read_secret(secret_path(name))
    except ValueError:
        return None


def has_secret(name: str) -> bool:
    try:
        return secret_path(name).exists()
    except ValueError:
        return False


def clear_secret(name: str) -> None:
    try:
        secret_path(name).unlink(missing_ok=True)
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# The OpenRouter key
# --------------------------------------------------------------------------- #
def save_api_key(key: str) -> None:
    """Persist the key encrypted at rest."""
    _write_secret(_SECRET_PATH, key)


def _load_saved_key() -> str | None:
    return _read_secret(_SECRET_PATH)


def load_api_key() -> str | None:
    """Resolve the key: env var first, then the saved encrypted value."""
    env = os.environ.get(API_KEY_ENV)
    if env:
        return env
    return _load_saved_key()


def has_saved_key() -> bool:
    return _SECRET_PATH.exists()


def clear_saved_key() -> None:
    _SECRET_PATH.unlink(missing_ok=True)
