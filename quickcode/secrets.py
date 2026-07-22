"""Encrypted at-rest storage for the OpenRouter API key.

The key may be provided two ways, checked in this order:
  1. the ``QUICKCODE_OPENROUTER_API_KEY`` environment variable (fixed name);
  2. a value saved from Settings, encrypted on disk.

On Windows the on-disk value is protected with DPAPI (CryptProtectData, tied to
the current user account) via ctypes — no third-party dependency. On other
platforms we fall back to a 0600 file with a light obfuscation (DPAPI has no
POSIX equivalent without extra deps; the file-permission is the real control).
"""

from __future__ import annotations

import base64
import os
import platform
from pathlib import Path

API_KEY_ENV = "QUICKCODE_OPENROUTER_API_KEY"

_SECRET_PATH = Path.home() / ".quickcode" / "openrouter.key"
_IS_WINDOWS = platform.system() == "Windows"


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


def save_api_key(key: str) -> None:
    """Persist the key encrypted at rest."""
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = key.encode("utf-8")
    if _IS_WINDOWS:
        payload = b"DPAPI:" + _dpapi(True, raw)
    else:
        # Not real encryption — the 0600 file permission is the control.
        payload = b"B64:" + base64.b64encode(raw)
    _SECRET_PATH.write_bytes(payload)
    try:
        os.chmod(_SECRET_PATH, 0o600)
    except OSError:
        pass


def _load_saved_key() -> str | None:
    if not _SECRET_PATH.exists():
        return None
    try:
        payload = _SECRET_PATH.read_bytes()
        if payload.startswith(b"DPAPI:"):
            return _dpapi(False, payload[6:]).decode("utf-8")
        if payload.startswith(b"B64:"):
            return base64.b64decode(payload[4:]).decode("utf-8")
    except Exception:
        return None
    return None


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
