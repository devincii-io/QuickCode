"""Update checking: version comparison, the throttled check, install-method
detection, and the download path's refusals.

Nothing here touches the network. Every HTTP interaction goes through an
``httpx.MockTransport`` built from the shape the real releases API actually
returns (recorded by hand from
``api.github.com/repos/devincii-io/QuickCode/releases/latest``), and nothing
downloaded in these tests is ever executed — the launch tests all assert a
refusal, which is the half of that path worth exercising anyway.

Every test also redirects ``~/.quickcode`` at ``tmp_path``: the check reads the
user settings layer and writes a cache file, and a test suite that read the
developer's real configuration would pass or fail depending on it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from quickcode import update

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway ``~/.quickcode`` for the cache, the setting and downloads."""
    root = tmp_path / "qc-home"
    root.mkdir()
    monkeypatch.setattr("quickcode.kernel.state.CONFIG_DIR", root)
    monkeypatch.setattr(update, "CACHE_PATH", root / "update-check.json")
    monkeypatch.setattr(update, "DOWNLOAD_DIR", root / "updates")
    monkeypatch.setattr(update, "installed_version", lambda: "2.0.0")
    return root


def release_payload(
    *, tag="v2.1.0", assets=("installer", "sums"), prerelease=False, draft=False
):
    """The subset of the real payload this module reads, in its real shape."""
    catalog = {
        "installer": ("QuickCode-Setup-2.1.0.exe", 2_100_000),
        "sums": ("SHA256SUMS.txt", 300),
        "wheel": ("quickcode-2.1.0-py3-none-any.whl", 400_000),
    }
    version = tag.lstrip("v")
    return {
        "tag_name": tag,
        "name": f"QuickCode {version}",
        "html_url": f"https://github.com/devincii-io/QuickCode/releases/tag/{tag}",
        "published_at": "2026-08-18T13:42:01Z",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": catalog[key][0],
                "size": catalog[key][1],
                "browser_download_url":
                    f"https://github.com/devincii-io/QuickCode/releases/download/"
                    f"{tag}/{catalog[key][0]}",
            }
            for key in assets
        ],
    }


class Recorder:
    """A MockTransport that counts what it was asked for."""

    def __init__(self, handler):
        self.requests: list[str] = []
        self._handler = handler
        self.transport = httpx.MockTransport(self._record)

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return self._handler(request)


def json_ok(payload):
    return Recorder(lambda _r: httpx.Response(200, json=payload))


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2.0.0", (2, 0, 0, 1, "")),
        ("v2.1.3", (2, 1, 3, 1, "")),
        ("v3.0.0-rc1", (3, 0, 0, 0, "rc1")),
        ("not-a-version", None),
        ("", None),
        ("0.0.0-dev", (0, 0, 0, 0, "dev")),
    ],
)
def test_parse_version(text, expected):
    assert update.parse_version(text) == expected


def test_is_newer_orders_releases_above_their_prereleases():
    assert update.is_newer("2.1.0", "2.0.0") is True
    assert update.is_newer("2.0.0", "2.0.0") is False
    assert update.is_newer("2.0.0", "2.1.0") is False
    # A release beats a pre-release of the same numbers, and a pre-release of a
    # higher number still beats the lower release.
    assert update.is_newer("2.0.0", "2.0.0-rc1") is True
    assert update.is_newer("2.1.0-rc1", "2.0.0") is True
    # Incomparable is None, never a guess.
    assert update.is_newer("2.1.0", "whatever") is None


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


async def test_available_when_the_release_is_newer(home):
    rec = json_ok(release_payload())
    status = await update.check(transport=rec.transport)
    assert status.state == "available"
    assert status.latest == "2.1.0"
    assert status.installed == "2.0.0"
    assert status.release.html_url.endswith("/tag/v2.1.0")
    assert len(rec.requests) == 1


async def test_current_when_the_release_is_the_installed_one(home):
    status = await update.check(transport=json_ok(release_payload(tag="v2.0.0")).transport)
    assert status.state == "current"
    assert status.update_available is False


async def test_the_check_is_throttled_and_persisted(home):
    rec = json_ok(release_payload())
    first = await update.check(transport=rec.transport, now=1000.0)
    assert first.cached is False
    # Well inside the interval: the stored answer comes back and nothing is
    # asked. This is the promise that opening the app twenty times in an
    # afternoon is one request.
    second = await update.check(transport=rec.transport, now=1000.0 + 60)
    assert second.cached is True
    assert second.state == "available"
    assert len(rec.requests) == 1
    # Past the interval it asks again.
    await update.check(transport=rec.transport, now=1000.0 + update.CHECK_INTERVAL_S + 1)
    assert len(rec.requests) == 2


async def test_force_bypasses_the_interval(home):
    rec = json_ok(release_payload())
    await update.check(transport=rec.transport, now=1000.0)
    await update.check(transport=rec.transport, now=1000.0, force=True)
    assert len(rec.requests) == 2


async def test_a_dead_network_is_an_answer_not_an_exception(home):
    def boom(_request):
        raise httpx.ConnectError("no route to host")

    status = await update.check(transport=httpx.MockTransport(boom))
    assert status.state == "unknown"
    assert status.error_kind == "offline"
    assert status.error
    # And it is written down, so the page can say when it last tried.
    assert (home / "update-check.json").exists()


async def test_a_failed_check_keeps_the_last_good_answer(home):
    rec = json_ok(release_payload())
    await update.check(transport=rec.transport, now=1000.0)

    def boom(_request):
        raise httpx.ConnectError("gone")

    status = await update.check(
        transport=httpx.MockTransport(boom), now=1000.0, force=True
    )
    assert status.state == "unknown"
    assert status.latest == "2.1.0"      # the last answer that arrived
    assert status.note


async def test_rate_limiting_is_named_and_backed_off(home):
    rec = Recorder(lambda _r: httpx.Response(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5000"},
        json={"message": "API rate limit exceeded"},
    ))
    status = await update.check(transport=rec.transport, now=1000.0)
    assert status.state == "unknown"
    assert status.error_kind == "rate_limited"
    assert status.retry_after == 5000.0
    # Before the reset, nothing is asked again — the retry interval alone
    # would have allowed it by now, and the back-off overrides that.
    await update.check(transport=rec.transport, now=1000.0 + update.RETRY_INTERVAL_S + 1)
    assert len(rec.requests) == 1
    # After it, one more attempt.
    await update.check(transport=rec.transport, now=5001.0)
    assert len(rec.requests) == 2


async def test_a_failed_check_is_retried_sooner_than_a_successful_one(home):
    """A minute without a network must not blind the app for six hours."""
    def boom(_request):
        raise httpx.ConnectError("gone")

    rec = Recorder(boom)
    await update.check(transport=rec.transport, now=1000.0)
    await update.check(transport=rec.transport, now=1000.0 + 60)
    assert len(rec.requests) == 1                      # still throttled
    await update.check(transport=rec.transport, now=1000.0 + update.RETRY_INTERVAL_S + 1)
    assert len(rec.requests) == 2
    assert update.RETRY_INTERVAL_S < update.CHECK_INTERVAL_S


async def test_a_403_that_is_not_a_rate_limit_says_so(home):
    rec = Recorder(lambda _r: httpx.Response(403, json={"message": "nope"}))
    status = await update.check(transport=rec.transport)
    assert status.error_kind == "http"


async def test_a_prerelease_is_never_offered(home):
    status = await update.check(
        transport=json_ok(release_payload(tag="v3.0.0", prerelease=True)).transport
    )
    assert status.state == "current"
    assert "pre-release" in status.note


async def test_a_release_without_artifacts_is_reported_but_not_offered(home, monkeypatch):
    monkeypatch.setattr(
        update, "detect_install",
        lambda *a, **k: update.InstallInfo("installer", "fake", app_dir="C:/x"),
    )
    status = await update.check(transport=json_ok(release_payload(assets=())).transport)
    assert status.state == "available"
    payload = status.to_json()
    assert payload["downloadable"] is False
    assert payload["artifacts_note"]


async def test_a_release_with_no_checksums_is_not_downloadable(home, monkeypatch):
    monkeypatch.setattr(
        update, "detect_install",
        lambda *a, **k: update.InstallInfo("installer", "fake", app_dir="C:/x"),
    )
    status = await update.check(
        transport=json_ok(release_payload(assets=("installer",))).transport
    )
    assert status.to_json()["downloadable"] is False


async def test_a_pip_install_is_never_downloadable(home, monkeypatch):
    monkeypatch.setattr(
        update, "detect_install", lambda *a, **k: update.InstallInfo("pip", "fake"),
    )
    payload = (await update.check(transport=json_ok(release_payload()).transport)).to_json()
    assert payload["downloadable"] is False
    assert any("uv pip install -U quickcode" in line for line in payload["instructions"])


async def test_malformed_json_is_reported_rather_than_raised(home):
    rec = Recorder(lambda _r: httpx.Response(200, text="<html>nope</html>"))
    status = await update.check(transport=rec.transport)
    assert status.state == "unknown"
    assert status.error_kind == "malformed"


async def test_an_unversioned_build_makes_no_claim(home, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.0.0-dev")
    status = await update.check(transport=json_ok(release_payload()).transport)
    assert status.state == "incomparable"
    assert status.update_available is False


# ---------------------------------------------------------------------------
# the off switch
# ---------------------------------------------------------------------------


async def test_disabling_stops_the_request_entirely(home):
    update.set_auto_check(False)
    rec = json_ok(release_payload())
    status = await update.check(transport=rec.transport)
    assert status.state == "disabled"
    assert status.auto_check is False
    assert rec.requests == []
    # Even forced: "check now" must not quietly re-enable a setting the user
    # switched off.
    await update.check(transport=rec.transport, force=True)
    assert rec.requests == []


def test_the_setting_is_written_at_user_scope_without_clobbering(home):
    settings = home / "settings.json"
    settings.write_text(json.dumps({
        "permissions": {"allow": ["read"]},
        "plugins": {"tool.bash": {"enabled": True}},
    }), encoding="utf-8")

    update.set_auto_check(False)
    raw = json.loads(settings.read_text(encoding="utf-8"))
    assert raw["permissions"] == {"allow": ["read"]}
    assert raw["plugins"]["tool.bash"] == {"enabled": True}
    assert raw["plugins"][update.PLUGIN_ID]["settings"][update.AUTO_CHECK_KEY] is False
    assert update.auto_check_enabled(None) is False

    update.set_auto_check(True)
    assert update.auto_check_enabled(None) is True


def test_disabling_the_plugin_also_stops_the_check(home):
    (home / "settings.json").write_text(
        json.dumps({"plugins": {update.PLUGIN_ID: {"enabled": False}}}), encoding="utf-8",
    )
    assert update.auto_check_enabled(None) is False


def test_the_default_is_on(home):
    assert update.auto_check_enabled(None) is True


# ---------------------------------------------------------------------------
# install-method detection
# ---------------------------------------------------------------------------


def test_the_windows_installer_layout_is_recognised(tmp_path, monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    app = tmp_path / "Programs" / "QuickCode"
    (app / "venv").mkdir(parents=True)
    (app / "unins000.exe").write_bytes(b"")
    info = update.detect_install(app / "venv")
    assert info.method == "installer"
    assert info.can_self_update is True
    assert Path(info.app_dir) == app


def test_a_venv_without_the_uninstaller_is_not_the_installer(tmp_path, monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr(update, "installed_version", lambda: "2.0.0")
    # The suite itself runs from an editable install of this repo, which is a
    # genuine "source" answer; this test is about the other branch.
    monkeypatch.setattr(update, "_editable_install", lambda: False)
    venv = tmp_path / "somewhere" / "venv"
    venv.mkdir(parents=True)
    # Only one of the two marks: this is an ordinary venv, and guessing
    # "installer" here would offer an .exe that updates nothing.
    info = update.detect_install(venv)
    assert info.method == "pip"
    assert info.can_self_update is False


def test_a_source_checkout_is_named_as_one(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.0.0-dev")
    info = update.detect_install(tmp_path)
    assert info.method == "source"
    assert info.can_self_update is False


def test_every_method_gets_real_instructions():
    for method in ("installer", "pip", "source", "unknown"):
        lines = update.manual_instructions(update.InstallInfo(method, ""))
        assert lines and all(line.strip() for line in lines)


# ---------------------------------------------------------------------------
# download and verify
# ---------------------------------------------------------------------------


INSTALLER_BYTES = b"MZ" + b"pretend installer" * 100
INSTALLER_NAME = "QuickCode-Setup-2.1.0.exe"


def download_transport(*, sums_body=None, body=INSTALLER_BYTES):
    if sums_body is None:
        digest = hashlib.sha256(body).hexdigest()
        sums_body = (
            f"{digest}  {INSTALLER_NAME}\n"
            f"{'0' * 64}  quickcode-2.1.0-py3-none-any.whl\n"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(update.CHECKSUMS_NAME):
            return httpx.Response(200, text=sums_body)
        if request.url.path.endswith(INSTALLER_NAME):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=release_payload())

    return Recorder(handler)


async def available_status(home, monkeypatch, **kwargs):
    monkeypatch.setattr(
        update, "detect_install",
        lambda *a, **k: update.InstallInfo("installer", "fake", app_dir="C:/x"),
    )
    return await update.check(transport=json_ok(release_payload(**kwargs)).transport)


async def test_a_matching_checksum_earns_the_real_filename(home, monkeypatch):
    status = await available_status(home, monkeypatch)
    rec = download_transport()
    result = await update.download_installer(
        status, transport=rec.transport, dest_dir=home / "updates",
    )
    saved = Path(result.path)
    assert saved.name == INSTALLER_NAME
    assert saved.read_bytes() == INSTALLER_BYTES
    assert result.sha256 == hashlib.sha256(INSTALLER_BYTES).hexdigest()
    assert result.size == len(INSTALLER_BYTES)
    # No .part survives, and the verification record sits beside the file.
    assert not (home / "updates" / (INSTALLER_NAME + ".part")).exists()
    assert saved.with_suffix(saved.suffix + ".sha256").read_text() == result.sha256
    # The checksums are fetched before the executable, always.
    assert rec.requests[0].endswith(update.CHECKSUMS_NAME)


async def test_a_checksum_mismatch_refuses_loudly_and_deletes_the_download(
    home, monkeypatch,
):
    status = await available_status(home, monkeypatch)
    # The manifest vouches for different bytes than the server sends.
    rec = download_transport(
        sums_body=f"{'a' * 64}  {INSTALLER_NAME}\n",
    )
    with pytest.raises(update.ChecksumMismatch) as excinfo:
        await update.download_installer(
            status, transport=rec.transport, dest_dir=home / "updates",
        )
    assert excinfo.value.expected == "a" * 64
    assert excinfo.value.actual == hashlib.sha256(INSTALLER_BYTES).hexdigest()
    assert "deleted" in str(excinfo.value)
    # Nothing is left behind under any name.
    assert list((home / "updates").iterdir()) == []


async def test_a_manifest_that_does_not_name_the_installer_downloads_nothing(
    home, monkeypatch,
):
    status = await available_status(home, monkeypatch)
    rec = download_transport(sums_body=f"{'b' * 64}  something-else.zip\n")
    with pytest.raises(update.UpdateError, match="does not list"):
        await update.download_installer(
            status, transport=rec.transport, dest_dir=home / "updates",
        )
    assert not any(r.endswith(INSTALLER_NAME) for r in rec.requests)


async def test_a_release_without_checksums_downloads_nothing(home, monkeypatch):
    status = await available_status(home, monkeypatch, assets=("installer",))
    rec = download_transport()
    with pytest.raises(update.UpdateError, match="SHA256SUMS"):
        await update.download_installer(
            status, transport=rec.transport, dest_dir=home / "updates",
        )
    assert rec.requests == []


async def test_a_pip_install_refuses_to_download_an_installer(home, monkeypatch):
    monkeypatch.setattr(
        update, "detect_install", lambda *a, **k: update.InstallInfo("pip", "fake"),
    )
    status = await update.check(transport=json_ok(release_payload()).transport)
    rec = download_transport()
    with pytest.raises(update.UpdateError, match="uv pip install"):
        await update.download_installer(
            status, transport=rec.transport, dest_dir=home / "updates",
        )
    assert rec.requests == []


async def test_a_plaintext_asset_url_is_refused_before_anything_is_fetched(
    home, monkeypatch,
):
    """The payload chooses the addresses, so the scheme is checked, not assumed.

    An http:// asset would be a hop anyone on the path could rewrite -- and the
    installer is the one download this app will execute. Nothing goes out at
    all: not even the checksums are fetched.
    """
    payload = release_payload()
    for asset in payload["assets"]:
        asset["browser_download_url"] = asset["browser_download_url"].replace(
            "https://", "http://",
        )
    monkeypatch.setattr(
        update, "detect_install",
        lambda *a, **k: update.InstallInfo("installer", "fake", app_dir="C:/x"),
    )
    status = await update.check(transport=json_ok(payload).transport)
    rec = download_transport()
    with pytest.raises(update.UpdateError, match="non-https"):
        await update.download_installer(
            status, transport=rec.transport, dest_dir=home / "updates",
        )
    assert rec.requests == []


def test_parse_checksums_skips_anything_it_does_not_fully_understand():
    parsed = update.parse_checksums(
        f"{'a' * 64}  good.exe\n"
        "not a checksum line\n"
        f"{'z' * 64}  bad-hex.exe\n"
        f"{'b' * 64} *binary-mode.exe extra\n"
        f"{'C' * 64}  Upper.exe\n"
    )
    assert parsed == {"good.exe": "a" * 64, "Upper.exe": "c" * 64}


# ---------------------------------------------------------------------------
# launching: every one of these is a refusal, and nothing is ever executed
# ---------------------------------------------------------------------------


def _verified_file(directory: Path) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / INSTALLER_NAME
    target.write_bytes(INSTALLER_BYTES)
    digest = hashlib.sha256(INSTALLER_BYTES).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(digest, encoding="ascii")
    return target, digest


def test_launching_refuses_a_file_outside_the_download_directory(home, tmp_path):
    stray, digest = _verified_file(tmp_path / "elsewhere")
    with pytest.raises(update.UpdateError, match="can be run"):
        update.launch_installer(stray, expected=digest, dest_dir=home / "updates")


def test_launching_refuses_a_file_with_no_verification_record(home):
    directory = home / "updates"
    directory.mkdir(parents=True)
    target = directory / INSTALLER_NAME
    target.write_bytes(INSTALLER_BYTES)
    with pytest.raises(update.UpdateError, match="verification record"):
        update.launch_installer(
            target, expected=hashlib.sha256(INSTALLER_BYTES).hexdigest(),
            dest_dir=directory,
        )


def test_launching_refuses_a_digest_the_user_was_not_shown(home):
    target, _ = _verified_file(home / "updates")
    with pytest.raises(update.UpdateError, match="does not match"):
        update.launch_installer(target, expected="f" * 64, dest_dir=home / "updates")


def test_launching_rehashes_and_refuses_bytes_that_changed_after_download(home):
    """The gap between "verified" and "run" is closed by re-reading the file."""
    target, digest = _verified_file(home / "updates")
    target.write_bytes(b"something else entirely")
    with pytest.raises(update.ChecksumMismatch):
        update.launch_installer(target, expected=digest, dest_dir=home / "updates")
    # And the tampered file does not survive the refusal.
    assert not target.exists()


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------


def make_client(tmp_path):
    from starlette.testclient import TestClient

    from quickcode.config import Config, Environment
    from quickcode.providers.base import ModelInfo
    from quickcode.server.app import create_app
    from quickcode.server.manager import ConversationManager

    class Provider:
        async def stream_chat(self, req):  # pragma: no cover - never called here
            if False:
                yield None

        async def list_models(self):
            return [ModelInfo(id="test/model", name="Test", context_length=1000)]

    env = Environment(
        cwd=str(tmp_path), platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-08-18", is_git_repo=False, git_branch="",
    )
    manager = ConversationManager(
        cwd=tmp_path, config=Config(), env=env, provider=Provider(),
    )
    app = create_app(manager, host="127.0.0.1", port=8642, token="")
    return TestClient(app, base_url="http://127.0.0.1:8642")


def test_get_update_reports_an_available_release(home, tmp_path, monkeypatch):
    async def fake_fetch(**_kwargs):
        return update.Release.from_payload(release_payload()), "", "", 0.0

    monkeypatch.setattr(update, "fetch_latest_release", fake_fetch)
    body = make_client(tmp_path).get("/api/update").json()
    assert body["state"] == "available"
    assert body["latest"] == "2.1.0"
    assert body["installed"] == "2.0.0"
    assert body["endpoint"] == update.RELEASES_API
    assert body["interval_s"] == update.CHECK_INTERVAL_S


def test_get_update_answers_200_when_the_network_is_dead(home, tmp_path, monkeypatch):
    """A failed check is a normal answer, so the UI can stay silent about it."""
    async def fake_fetch(**_kwargs):
        return None, "Could not reach github.com.", "offline", 0.0

    monkeypatch.setattr(update, "fetch_latest_release", fake_fetch)
    response = make_client(tmp_path).get("/api/update")
    assert response.status_code == 200
    assert response.json()["state"] == "unknown"
    assert response.json()["error_kind"] == "offline"


def test_put_update_settings_switches_it_off(home, tmp_path, monkeypatch):
    async def fake_fetch(**_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the check ran while it was disabled")

    monkeypatch.setattr(update, "fetch_latest_release", fake_fetch)
    client = make_client(tmp_path)
    body = client.put("/api/update/settings", json={"check_automatically": False}).json()
    assert body["state"] == "disabled"
    assert body["auto_check"] is False
    assert client.get("/api/update").json()["auto_check"] is False


def test_put_update_settings_rejects_a_body_that_is_not_a_boolean(home, tmp_path):
    client = make_client(tmp_path)
    assert client.put("/api/update/settings", json={}).status_code == 400
    assert client.put(
        "/api/update/settings", json={"check_automatically": "maybe"}
    ).status_code == 400


def test_install_requires_an_explicit_confirmation_and_a_digest(home, tmp_path):
    client = make_client(tmp_path)
    target, digest = _verified_file(home / "updates")
    # No confirmation at all.
    assert client.post("/api/update/install", json={}).status_code == 400
    # Confirmed, but nothing named.
    assert client.post(
        "/api/update/install", json={"confirm": True}
    ).status_code == 400
    # Confirmed and named, but the digest is not the one on record: refused
    # before anything is started.
    assert client.post("/api/update/install", json={
        "confirm": True, "path": str(target), "sha256": "f" * 64,
    }).status_code == 400
    assert target.exists()
