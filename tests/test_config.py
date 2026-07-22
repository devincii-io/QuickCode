from pathlib import Path

from quickcode.config import CatalogEntry, Config, Profile


def test_resolve_falls_back_to_role_default():
    p = Profile()
    assert p.resolve("orchestrator") == p.orchestrator_model
    assert p.resolve("worker") == p.worker_model


def test_resolve_prefers_catalog_by_tier():
    p = Profile(
        catalog=[
            CatalogEntry(id="vendor/cheap", tier="cheap", roles=["worker"]),
            CatalogEntry(id="vendor/quality", tier="quality", roles=["orchestrator", "worker"]),
        ]
    )
    assert p.resolve("worker", tier="cheap") == "vendor/cheap"
    assert p.resolve("orchestrator", tier="quality") == "vendor/quality"
    # role filter excludes cheap (worker-only) from orchestrator
    assert p.resolve("orchestrator") == "vendor/quality"


def test_save_load_roundtrip(tmp_path: Path):
    cfg = Config()
    cfg.profiles["default"].catalog = [
        CatalogEntry(id="vendor/x", tier="balanced", roles=["worker"], label="X")
    ]
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = Config.load(path)
    entry = loaded.profiles["default"].catalog[0]
    assert entry.id == "vendor/x"
    assert entry.tier == "balanced"
    assert entry.roles == ["worker"]
