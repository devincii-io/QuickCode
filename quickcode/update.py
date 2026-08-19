"""Update checking — the only request QuickCode makes to the internet on its
own initiative, and everything that decides what "update" can honestly mean.

Three separate promises are kept here, and they are worth stating apart:

**It is one plain GET, and nothing rides along.** The check is an
unauthenticated ``GET`` of ``/repos/<owner>/<repo>/releases/latest``. No
Authorization header, no cookies, no query string, no body. The only header
that is not a constant would be a version-bearing User-Agent, so this does not
send one: GitHub requires *a* User-Agent, it gets the fixed string
``QuickCode``, and it therefore learns nothing about this install beyond the
fact that some IP asked what the latest release is. Nothing about the machine,
the project, the session, the model or the user leaves this process. There is
no second endpoint and no fallback host.

**It is rate-limited by us, not only by GitHub.** Unauthenticated callers get
60 requests per hour per IP. The check runs at most once every
``CHECK_INTERVAL_S`` and the last-check time is persisted, so opening the app
twenty times in an afternoon is one request. A 403/429 that carries
``x-ratelimit-remaining: 0`` is honoured: nothing is asked again until the
reset time GitHub named.

**It never executes anything on its own.** How QuickCode was installed decides
what can be offered, and guessing wrong is worse than offering nothing (see
``detect_install``). A pip/uv install is told the command, because a process
cannot reliably replace the package it is currently running. Only the Windows
installer layout gets a downloadable artifact, and that path is deliberately
narrow, in the spirit of ``security/trust.py``:

  1. ``SHA256SUMS.txt`` is fetched from the release **first**;
  2. the installer is streamed to ``~/.quickcode/updates/<name>.part`` while
     being hashed;
  3. a digest that does not match refuses loudly and **deletes the download** —
     the partial file never gets its real name, so nothing can be run by
     accident;
  4. a digest that matches is renamed into place and recorded beside the file;
  5. launching re-hashes the bytes on disk and compares against both that
     record and the digest the user was shown before clicking. Nothing runs on
     a checksum this module has not verified twice.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from quickcode.config import CONFIG_DIR

log = logging.getLogger("quickcode.update")

OWNER_REPO = "devincii-io/QuickCode"
RELEASES_API = f"https://api.github.com/repos/{OWNER_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{OWNER_REPO}/releases"

# The plugin this setting belongs to, in the manifest's vocabulary. Read
# through kernel.state so the user layer and a project override both apply.
PLUGIN_ID = "runtime.updates"
AUTO_CHECK_KEY = "check_automatically"

# Six hours. "Never more than once every few hours" is the promise; the number
# lives here so the prose, the cache and the route cannot disagree.
CHECK_INTERVAL_S = 6 * 60 * 60
# A check that did not complete is retried sooner than one that did. The long
# interval exists so a working answer is not asked for again; it is not a
# reason to be blind for six hours because a laptop was on a train for one
# minute. Rate limiting is the exception and honours GitHub's own reset time
# instead of this.
RETRY_INTERVAL_S = 30 * 60

# What ``quickcode.__init__`` answers when there is no installed distribution
# to read. It parses as a version and must not be compared as one.
DEV_VERSION = "0.0.0-dev"

# Where the last answer and the last-check time live. Deliberately not in
# config.json: this is a cache, not configuration, and deleting it costs
# nothing but one request.
CACHE_PATH = CONFIG_DIR / "update-check.json"
DOWNLOAD_DIR = CONFIG_DIR / "updates"
CACHE_VERSION = 1

CHECK_TIMEOUT_S = 10.0
DOWNLOAD_TIMEOUT_S = 300.0
# The installer carries the frozen application folder — a Python runtime, the
# dependencies and the frontend — currently a few tens of MB compressed. The cap
# is not a guess about the file, it is a refusal to stream an unbounded body
# onto the user's disk.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024

# Fixed for every install. See the module docstring: a version in here would be
# the one thing in the request that varied per user.
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "QuickCode",
}

INSTALLER_RE = re.compile(r"^QuickCode-Setup-.+\.exe$", re.IGNORECASE)
CHECKSUMS_NAME = "SHA256SUMS.txt"

PIP_COMMAND = "uv pip install -U quickcode"
PIP_COMMAND_ALT = "pip install -U quickcode"


class UpdateError(Exception):
    """A refusal the UI should render as a message, not a stack trace."""


class ChecksumMismatch(UpdateError):
    """The downloaded bytes are not the bytes the release vouches for.

    Raised only after the partial download has already been deleted.
    """

    def __init__(self, name: str, expected: str, actual: str) -> None:
        self.name = name
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{name} did not match its published SHA-256. Expected {expected}, "
            f"got {actual}. The download was deleted and nothing was run."
        )


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+.]?(.+))?$")


def parse_version(text: str) -> tuple[int, int, int, int, str] | None:
    """``"v2.1.0"`` → ``(2, 1, 0, 1, "")``; ``None`` when it is not a version.

    The fourth element ranks a release above any pre-release of the same
    numbers (0 for ``2.1.0-rc1``, 1 for ``2.1.0``), which is the whole reason
    this is not a plain three-tuple.
    """
    match = _VERSION_RE.match((text or "").strip())
    if match is None:
        return None
    major, minor, patch, suffix = match.groups()
    suffix = (suffix or "").strip()
    return (int(major), int(minor), int(patch), 0 if suffix else 1, suffix)


def is_newer(latest: str, installed: str) -> bool | None:
    """``True``/``False``, or ``None`` when the two cannot be compared."""
    a, b = parse_version(latest), parse_version(installed)
    if a is None or b is None:
        return None
    return a > b


def installed_version() -> str:
    """The running version, from the installed distribution metadata.

    ``quickcode.__init__`` answers ``0.0.0-dev`` from a source checkout, where
    there is no distribution to read and therefore nothing to compare against.
    """
    from quickcode import __version__

    return __version__


# --------------------------------------------------------------------------
# How this copy of QuickCode got here
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallInfo:
    """Which of the shipping shapes this process is running out of.

    ``method`` is one of:

    ``installer``  the Windows Inno Setup install: the frozen application
                   folder beside the uninstaller, normally under
                   ``%LOCALAPPDATA%\\Programs\\QuickCode``.
    ``pip``        installed as a package into some environment.
    ``source``     a checkout, or an editable install of one.
    ``unknown``    detection could not tell, which is said out loud rather
                   than resolved by assuming the convenient answer.
    """

    method: str
    detail: str
    app_dir: str = ""
    prefix: str = ""

    @property
    def can_self_update(self) -> bool:
        return self.method == "installer"

    def to_json(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "detail": self.detail,
            "app_dir": self.app_dir,
            "prefix": self.prefix,
            "can_self_update": self.can_self_update,
        }


def _uninstaller_beside(app: Path) -> bool:
    """Whether an Inno Setup uninstaller sits in ``app``.

    That file is written by the installer and by nothing else, so its presence
    is evidence rather than inference — which is the whole reason both layout
    checks below insist on it.
    """
    try:
        return any(app.glob("unins*.exe"))
    except OSError:
        return False


def _frozen_app_dir(executable: str | os.PathLike[str] | None = None) -> Path | None:
    """``<app>`` when this process is the frozen build the installer ships.

    The shipping shape since the move to PyInstaller: ``QuickCodeApp.exe`` and
    ``quickcode.exe`` sit directly in the install directory, next to the
    uninstaller, with the Python runtime under ``_internal``. There is no venv
    and no ``sys.prefix`` worth reading — a frozen process reports the
    application folder there — so this looks at the executable instead.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    try:
        app = Path(executable or sys.executable).resolve().parent
    except OSError:
        return None
    return app if _uninstaller_beside(app) else None


def _inno_app_dir(prefix: Path) -> Path | None:
    """``<app>`` when ``prefix`` is the venv a pre-frozen installer built.

    Two independent marks are required, because either alone is a coincidence
    waiting to happen: the venv is named ``venv`` and sits directly under a
    directory holding an Inno Setup uninstaller (``unins000.exe``). That
    uninstaller is written by the installer and by nothing else, so its
    presence is evidence rather than inference.
    """
    if os.name != "nt":
        return None
    if prefix.name.lower() != "venv":
        return None
    app = prefix.parent
    return app if _uninstaller_beside(app) else None


def _editable_install() -> bool:
    """True when the distribution points at a working tree (``pip install -e``)."""
    try:
        from importlib.metadata import distribution

        raw = distribution("quickcode").read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return bool(isinstance(info, dict) and (info.get("dir_info") or {}).get("editable"))


def detect_install(prefix: str | os.PathLike[str] | None = None) -> InstallInfo:
    """Work out what an update could mean here. Never raises."""
    try:
        root = Path(prefix) if prefix is not None else Path(sys.prefix)
        app = _frozen_app_dir()
        if app is not None:
            return InstallInfo(
                method="installer",
                detail=(
                    "Installed by the Windows installer: the QuickCode "
                    f"application folder at {app}, beside its uninstaller."
                ),
                app_dir=str(app),
                prefix=str(root),
            )
        if getattr(sys, "frozen", False):
            # The application folder, but nobody installed it -- an unpacked
            # release, or a build straight out of dist/. There is no installer
            # to hand it, and "pip install -U" would be a lie, so nothing is
            # offered and the reason is said out loud.
            return InstallInfo(
                method="unknown",
                detail=(
                    "Running the frozen application folder without an "
                    "uninstaller beside it — a portable or unpacked copy "
                    "rather than an install."
                ),
                prefix=str(root),
            )
        app = _inno_app_dir(root)
        if app is not None:
            return InstallInfo(
                method="installer",
                detail=(
                    "Installed by an older Windows installer: a private virtual "
                    f"environment at {root}, beside the uninstaller in {app}."
                ),
                app_dir=str(app),
                prefix=str(root),
            )
        if installed_version() == DEV_VERSION:
            return InstallInfo(
                method="source",
                detail="Running from a source checkout — there is no installed "
                       "distribution to compare against or replace.",
                prefix=str(root),
            )
        if _editable_install():
            return InstallInfo(
                method="source",
                detail="Installed in editable mode from a working tree; the "
                       "checkout is the thing to update, with git.",
                prefix=str(root),
            )
        return InstallInfo(
            method="pip",
            detail=f"Installed as a package into the environment at {root}.",
            prefix=str(root),
        )
    except Exception as exc:  # detection is never worth a 500
        log.debug("install detection failed: %s", exc)
        return InstallInfo(
            method="unknown",
            detail="Could not tell how this copy of QuickCode was installed.",
        )


def manual_instructions(info: InstallInfo) -> list[str]:
    """What to actually do, for the cases where nothing can be done for you."""
    if info.method == "pip":
        return [
            f"Run {PIP_COMMAND} in the environment QuickCode is installed in "
            f"(or {PIP_COMMAND_ALT}), then restart the app. A running process "
            "cannot reliably replace the package it is executing.",
        ]
    if info.method == "source":
        return [
            "Pull the checkout and reinstall it: git pull, then "
            "uv sync (or pip install -e .).",
        ]
    if info.method == "installer":
        return [
            "Download the installer below and run it. It installs over the "
            "existing copy and keeps your configuration.",
        ]
    return [
        "How this copy was installed could not be determined, so nothing is "
        "offered automatically. Download the release yourself from "
        f"{RELEASES_PAGE} and install it the same way you installed this one.",
    ]


# --------------------------------------------------------------------------
# The setting, and the persisted last-check time
# --------------------------------------------------------------------------


def auto_check_enabled(cwd: Path | None = None) -> bool:
    """Whether the automatic check is on. Defaults to on, plainly described.

    Two off switches, both already part of the plugin vocabulary: the setting
    below, and disabling the ``runtime.updates`` plugin outright.
    """
    try:
        from quickcode.kernel.state import disabled_plugin_ids, plugin_setting

        if PLUGIN_ID in disabled_plugin_ids(cwd):
            return False
        value = plugin_setting(cwd, PLUGIN_ID, AUTO_CHECK_KEY, True)
    except Exception as exc:  # an unreadable settings file must not decide this
        log.debug("could not read the update setting: %s", exc)
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def set_auto_check(enabled: bool) -> bool:
    """Write the setting at **user** scope, and return what was written.

    ``kernel.state.save_entry`` writes the project file, which is right for a
    knob tuned per repository and wrong for this one: whether this install
    talks to github.com is not a property of the directory that happens to be
    open. It is written the same way (read, merge, write back, nothing else in
    the file touched) into ``~/.quickcode/settings.json``, which
    ``load_state`` reads underneath the project layer — so a project can still
    pin it off, and cannot turn it on for you.
    """
    from quickcode.kernel.state import PLUGINS_KEY, user_settings_path

    path = user_settings_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError(f"could not read {path}: {exc}") from exc

    section = raw.get(PLUGINS_KEY)
    if not isinstance(section, dict):
        section = {}
    entry = section.get(PLUGIN_ID)
    if not isinstance(entry, dict):
        entry = {}
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    settings[AUTO_CHECK_KEY] = bool(enabled)
    entry["settings"] = settings
    section[PLUGIN_ID] = entry
    raw[PLUGINS_KEY] = section

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    except OSError as exc:
        raise UpdateError(f"could not write {path}: {exc}") from exc
    return bool(enabled)


def _read_cache(path: Path | None = None) -> dict[str, Any]:
    p = path or CACHE_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict[str, Any], path: Path | None = None) -> None:
    p = path or CACHE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:  # a cache that cannot be written costs one request
        log.debug("could not write the update cache: %s", exc)


# --------------------------------------------------------------------------
# The release, and the status the UI renders
# --------------------------------------------------------------------------


@dataclass
class Release:
    """The handful of fields from the API payload that are actually used."""

    tag: str = ""
    version: str = ""
    name: str = ""
    html_url: str = RELEASES_PAGE
    published_at: str = ""
    prerelease: bool = False
    draft: bool = False
    # asset name -> {"url": …, "size": …}
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> Release:
        if not isinstance(payload, dict):
            raise UpdateError("the releases API answered with something that is not a release")
        tag = str(payload.get("tag_name") or "")
        assets: dict[str, dict[str, Any]] = {}
        for asset in payload.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            url = asset.get("browser_download_url")
            if isinstance(name, str) and isinstance(url, str):
                assets[name] = {"url": url, "size": asset.get("size")}
        return cls(
            tag=tag,
            version=tag.lstrip("vV"),
            name=str(payload.get("name") or tag),
            html_url=str(payload.get("html_url") or RELEASES_PAGE),
            published_at=str(payload.get("published_at") or ""),
            prerelease=bool(payload.get("prerelease")),
            draft=bool(payload.get("draft")),
            assets=assets,
        )

    def installer_asset(self) -> tuple[str, dict[str, Any]] | None:
        for name, info in self.assets.items():
            if INSTALLER_RE.match(name):
                return name, info
        return None

    def checksums_asset(self) -> dict[str, Any] | None:
        return self.assets.get(CHECKSUMS_NAME)

    def to_json(self) -> dict[str, Any]:
        installer = self.installer_asset()
        return {
            "tag": self.tag,
            "version": self.version,
            "name": self.name,
            "html_url": self.html_url,
            "published_at": self.published_at,
            "prerelease": self.prerelease,
            "assets": sorted(self.assets),
            "installer": (
                {"name": installer[0], "size": installer[1].get("size")}
                if installer else None
            ),
            "has_checksums": self.checksums_asset() is not None,
        }


@dataclass
class UpdateStatus:
    """What the Install page and the top-bar chip render, in one object.

    ``state`` is the whole vocabulary:

    ``available``     a newer release exists.
    ``current``       nothing newer, or the newest is a pre-release we decline.
    ``incomparable``  a release was read but the running version is not a
                      version (a source checkout), so no claim is made.
    ``disabled``      automatic checking is off and this was not forced.
    ``unknown``       the check did not complete. ``error`` says why, and this
                      is the state a dead network produces — silent in the
                      chrome, spelled out on the Install page.
    """

    state: str = "unknown"
    installed: str = ""
    latest: str = ""
    release: Release | None = None
    install: InstallInfo | None = None
    checked_at: float = 0.0
    cached: bool = False
    error: str = ""
    error_kind: str = ""
    note: str = ""
    auto_check: bool = True
    retry_after: float = 0.0

    @property
    def update_available(self) -> bool:
        return self.state == "available"

    def to_json(self) -> dict[str, Any]:
        info = self.install or detect_install()
        installer = self.release.installer_asset() if self.release else None
        # "There is a newer version AND there is something to hand you" is a
        # different question from "is there a newer version", and conflating
        # them is how an updater ends up offering a button that cannot work.
        downloadable = bool(
            self.state == "available"
            and info.can_self_update
            and installer is not None
            and self.release is not None
            and self.release.checksums_asset() is not None
        )
        artifacts_note = ""
        if self.state == "available" and info.can_self_update and not downloadable:
            artifacts_note = (
                "This release does not carry both a Windows installer and a "
                f"{CHECKSUMS_NAME} yet, so there is nothing to verify and "
                "nothing is offered. The release notes are linked above."
            )
        return {
            "state": self.state,
            "update_available": self.update_available,
            "installed": self.installed,
            "latest": self.latest,
            "release": self.release.to_json() if self.release else None,
            "install": info.to_json(),
            "instructions": manual_instructions(info),
            "downloadable": downloadable,
            "artifacts_note": artifacts_note,
            "checked_at": self.checked_at,
            "cached": self.cached,
            "error": self.error,
            "error_kind": self.error_kind,
            "note": self.note,
            "auto_check": self.auto_check,
            "retry_after": self.retry_after,
            "interval_s": CHECK_INTERVAL_S,
            "endpoint": RELEASES_API,
            "releases_page": RELEASES_PAGE,
        }

    def cache_entry(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "checked_at": self.checked_at,
            "installed": self.installed,
            "state": self.state,
            "latest": self.latest,
            "error": self.error,
            "error_kind": self.error_kind,
            "note": self.note,
            "retry_after": self.retry_after,
            "release": {
                "tag": self.release.tag,
                "version": self.release.version,
                "name": self.release.name,
                "html_url": self.release.html_url,
                "published_at": self.release.published_at,
                "prerelease": self.release.prerelease,
                "draft": self.release.draft,
                "assets": self.release.assets,
            } if self.release else None,
        }

    @classmethod
    def from_cache_entry(cls, entry: dict[str, Any], *, installed: str) -> UpdateStatus | None:
        if not isinstance(entry, dict) or entry.get("version") != CACHE_VERSION:
            return None
        # A cache written by a different build describes a comparison that is
        # no longer the one being made.
        if entry.get("installed") != installed:
            return None
        raw = entry.get("release")
        release = None
        if isinstance(raw, dict):
            release = Release(
                tag=str(raw.get("tag") or ""),
                version=str(raw.get("version") or ""),
                name=str(raw.get("name") or ""),
                html_url=str(raw.get("html_url") or RELEASES_PAGE),
                published_at=str(raw.get("published_at") or ""),
                prerelease=bool(raw.get("prerelease")),
                draft=bool(raw.get("draft")),
                assets=raw.get("assets") if isinstance(raw.get("assets"), dict) else {},
            )
        return cls(
            state=str(entry.get("state") or "unknown"),
            installed=installed,
            latest=str(entry.get("latest") or ""),
            release=release,
            checked_at=float(entry.get("checked_at") or 0.0),
            cached=True,
            error=str(entry.get("error") or ""),
            error_kind=str(entry.get("error_kind") or ""),
            note=str(entry.get("note") or ""),
            retry_after=float(entry.get("retry_after") or 0.0),
        )


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------


def _client(transport: httpx.AsyncBaseTransport | None, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        headers=_HEADERS,
        follow_redirects=False,
        trust_env=False,   # no proxy or auth picked up from the environment
    )


def _rate_limit_reset(response: httpx.Response) -> float:
    with contextlib.suppress(TypeError, ValueError):
        return float(response.headers.get("x-ratelimit-reset") or 0)
    return 0.0


async def fetch_latest_release(
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[Release | None, str, str, float]:
    """One unauthenticated GET. Returns ``(release, error, kind, retry_after)``.

    Never raises for a network condition: "could not reach github.com" is an
    ordinary outcome of this function, not an exception, because it is an
    ordinary condition for the user.
    """
    try:
        async with _client(transport, CHECK_TIMEOUT_S) as client:
            response = await client.get(RELEASES_API)
    except httpx.TimeoutException:
        return None, "The check timed out.", "offline", 0.0
    except httpx.HTTPError as exc:
        return None, f"Could not reach github.com ({exc.__class__.__name__}).", "offline", 0.0

    if response.status_code in (403, 429):
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining == "0":
            reset = _rate_limit_reset(response)
            return None, (
                "GitHub's unauthenticated rate limit (60 requests per hour per "
                "IP address) is exhausted for this network."
            ), "rate_limited", reset
        return None, f"GitHub refused the request (HTTP {response.status_code}).", "http", 0.0
    if response.status_code == 404:
        return None, "No published release was found for this repository.", "http", 0.0
    if response.status_code >= 400:
        return None, f"GitHub answered HTTP {response.status_code}.", "http", 0.0

    try:
        payload = response.json()
    except ValueError:
        return None, "GitHub's answer was not JSON.", "malformed", 0.0
    try:
        return Release.from_payload(payload), "", "", 0.0
    except UpdateError as exc:
        return None, str(exc), "malformed", 0.0


async def check(
    *,
    cwd: Path | None = None,
    force: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    cache_path: Path | None = None,
    now: float | None = None,
) -> UpdateStatus:
    """The status to show, asking github.com only when that is due.

    ``force`` is the explicit "check now" button: it ignores the interval and
    the retry-after, but *not* the off switch — a disabled check is disabled,
    and a button that quietly re-enabled it would make the setting a lie.
    """
    stamp = time.time() if now is None else now
    installed = installed_version()
    info = detect_install()
    enabled = auto_check_enabled(cwd)
    cached = UpdateStatus.from_cache_entry(_read_cache(cache_path), installed=installed)
    if cached is not None:
        cached.install = info
        cached.auto_check = enabled

    if not enabled:
        # Still show the last answer if there is one; it is a fact that was
        # already learned, not a new request.
        if cached is not None:
            cached.state = "disabled" if cached.state == "unknown" else cached.state
            cached.note = ("Automatic checking is off. This is the last answer, "
                           "from before it was switched off.")
            return cached
        return UpdateStatus(
            state="disabled", installed=installed, install=info, auto_check=False,
            note="Automatic checking is off. Nothing has been sent to github.com.",
        )

    if cached is not None and not force:
        interval = RETRY_INTERVAL_S if cached.state == "unknown" else CHECK_INTERVAL_S
        fresh = stamp - cached.checked_at < interval
        backing_off = cached.retry_after > stamp
        if fresh or backing_off:
            return cached

    release, error, kind, retry_after = await fetch_latest_release(transport=transport)
    status = UpdateStatus(
        installed=installed, install=info, checked_at=stamp, auto_check=enabled,
    )
    if release is None:
        status.state = "unknown"
        status.error = error
        status.error_kind = kind
        status.retry_after = retry_after
        # A failed check must not cost the last good answer: the Install page
        # can then say "the last time this worked, 2.0.0 was current".
        if cached is not None and cached.release is not None:
            status.release = cached.release
            status.latest = cached.latest
            status.note = "Showing the last answer that arrived."
        _write_cache(status.cache_entry(), cache_path)
        return status

    status.release = release
    status.latest = release.version

    if release.draft or release.prerelease:
        # /releases/latest is documented to exclude both; if one arrives
        # anyway it is not something to push at people.
        status.state = "current"
        status.note = (
            f"The newest release ({release.tag or 'untagged'}) is marked "
            "pre-release, so it is not offered as an update."
        )
        _write_cache(status.cache_entry(), cache_path)
        return status

    # The dev sentinel parses as 0.0.0, which would make every release look
    # newer than a checkout that may well be ahead of all of them. "I cannot
    # tell" is the only true answer there.
    newer = None if installed == DEV_VERSION else is_newer(release.version, installed)
    if newer is None:
        status.state = "incomparable"
        status.note = (
            f"The running version reads {installed!r}, which is not a release "
            "version, so no comparison is made."
        )
    elif newer:
        status.state = "available"
    else:
        status.state = "current"
    _write_cache(status.cache_entry(), cache_path)
    return status


# --------------------------------------------------------------------------
# Download, verify, and only then offer to run
# --------------------------------------------------------------------------


@dataclass
class Download:
    path: str
    name: str
    sha256: str
    size: int

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "name": self.name, "sha256": self.sha256, "size": self.size}


def parse_checksums(text: str) -> dict[str, str]:
    """``SHA256SUMS.txt`` → ``{filename: digest}``.

    The coreutils shape: ``<64 hex>  <name>``. A line that is not exactly that
    is skipped rather than guessed at — a manifest we half-understand is not a
    manifest we may verify against.
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        if len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
            out[name] = digest.lower()
    return out


def _https(url: str, what: str) -> str:
    """Refuse an asset URL that is not https, before anything fetches it.

    The addresses come out of the API payload rather than from this file, and
    the payload arrives over TLS from a pinned host, so this should never fire.
    It is one comparison, and what it rules out is the whole class of "the
    answer told us to fetch the installer from somewhere else" -- including a
    plaintext hop that anyone on the path could rewrite.
    """
    if not str(url).lower().startswith("https://"):
        raise UpdateError(f"the release names a non-https URL for {what}; refused")
    return str(url)


async def _get_text(url: str, transport: httpx.AsyncBaseTransport | None) -> str:
    async with _client(transport, CHECK_TIMEOUT_S) as client:
        response = await client.get(url, follow_redirects=True)
        if response.status_code >= 400:
            raise UpdateError(f"could not fetch {url} (HTTP {response.status_code})")
        return response.text


def _record_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".sha256")


async def download_installer(
    status: UpdateStatus,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    dest_dir: Path | None = None,
) -> Download:
    """Fetch the release's installer and verify it against the release's sums.

    The order is the point. ``SHA256SUMS.txt`` is fetched **first**, so the
    expected digest is known before a single byte of executable is written; a
    manifest that does not name the installer ends the operation with nothing
    downloaded. The body streams to ``<name>.part`` and is hashed as it
    arrives. Only a matching digest earns the real filename.
    """
    if status.release is None:
        raise UpdateError("there is no release to download")
    info = status.install or detect_install()
    if not info.can_self_update:
        raise UpdateError(
            "this install is not the Windows installer layout, so downloading "
            "an installer would not update it. " + manual_instructions(info)[0]
        )
    installer = status.release.installer_asset()
    if installer is None:
        raise UpdateError("this release has no Windows installer attached")
    sums_asset = status.release.checksums_asset()
    if sums_asset is None:
        raise UpdateError(
            f"this release has no {CHECKSUMS_NAME}, so the download could not "
            "be verified — nothing was fetched"
        )

    name, asset = installer
    # Both addresses are checked before either is fetched, so a release that
    # names a plaintext URL is refused without a request having gone out.
    sums_url = _https(sums_asset["url"], CHECKSUMS_NAME)
    asset_url = _https(asset["url"], name)
    sums = parse_checksums(await _get_text(sums_url, transport))
    expected = sums.get(name)
    if not expected:
        raise UpdateError(
            f"{CHECKSUMS_NAME} does not list {name}, so there is nothing to "
            "verify it against — nothing was downloaded"
        )

    directory = Path(dest_dir) if dest_dir is not None else DOWNLOAD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    partial = directory / (name + ".part")
    with contextlib.suppress(OSError):
        partial.unlink()

    digest = hashlib.sha256()
    total = 0
    try:
        async with _client(transport, DOWNLOAD_TIMEOUT_S) as client:
            async with client.stream("GET", asset_url, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise UpdateError(
                        f"could not download {name} (HTTP {response.status_code})"
                    )
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise UpdateError(
                                f"{name} is larger than the {MAX_DOWNLOAD_BYTES} byte "
                                "cap; the download was abandoned"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
    except (httpx.HTTPError, OSError) as exc:
        _discard(partial)
        raise UpdateError(f"could not download {name}: {exc}") from exc
    except UpdateError:
        _discard(partial)
        raise

    actual = digest.hexdigest()
    if actual != expected:
        # Loud, and gone. The bytes are deleted before the caller is told, so
        # there is no window in which a mismatched installer exists on disk
        # under a name anything would run.
        _discard(partial)
        log.error("checksum mismatch for %s: expected %s, got %s", name, expected, actual)
        raise ChecksumMismatch(name, expected, actual)

    try:
        partial.replace(target)
        _record_path(target).write_text(actual, encoding="ascii")
    except OSError as exc:
        _discard(partial)
        _discard(target)
        raise UpdateError(f"could not save the verified download: {exc}") from exc

    return Download(path=str(target), name=name, sha256=actual, size=total)


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(path: Path, expected: str) -> str:
    """Re-hash a file on disk and refuse unless it matches ``expected``.

    Called again at launch, not only after downloading. The gap between "we
    verified it" and "we ran it" is exactly the gap this closes, and re-reading
    a two-megabyte file is not a cost worth trading it for.
    """
    if not path.is_file():
        raise UpdateError(f"{path} is not there any more")
    actual = sha256_file(path)
    if actual != (expected or "").lower():
        _discard(path)
        _discard(_record_path(path))
        raise ChecksumMismatch(path.name, expected, actual)
    return actual


def launch_installer(
    path: Path, *, expected: str, dest_dir: Path | None = None
) -> dict[str, Any]:
    """Run a verified installer, after verifying it once more.

    ``expected`` is the digest the user was shown next to the button they
    clicked, so agreeing to it is agreeing to specific bytes rather than to a
    filename. It must also match the digest recorded beside the file when it
    was downloaded — a record this process wrote and nothing else touches.
    """
    path = Path(path).resolve()
    # Only ever a file this module downloaded. The API is loopback-and-token
    # only, but "run the path in this JSON body" is not a shape worth having
    # in the process at all, so the directory is fixed rather than trusted.
    allowed = (Path(dest_dir) if dest_dir is not None else DOWNLOAD_DIR).resolve()
    if path.parent != allowed:
        raise UpdateError(
            f"only downloads under {allowed} can be run; {path} is not one"
        )
    record = _record_path(path)
    try:
        recorded = record.read_text(encoding="ascii").strip().lower()
    except OSError as exc:
        raise UpdateError(
            f"{path.name} has no verification record beside it, so it is not a "
            "download this app checked. It will not be run."
        ) from exc
    if recorded != (expected or "").strip().lower():
        raise UpdateError(
            "the checksum offered does not match the one recorded for this "
            "download; nothing was run"
        )
    verify_download(path, recorded)

    if os.name != "nt":
        raise UpdateError("the Windows installer can only be run on Windows")
    # Detached, and never with a shell: the argv is one path this module wrote.
    subprocess.Popen([str(path)], close_fds=True)  # noqa: S603
    return {"launched": True, "path": str(path), "sha256": recorded}
