"""``resolve_orchestrator``: the call every opener of a session used to spell out."""

from __future__ import annotations

import pytest

from quickcode.kernel import state as state_store
from quickcode.kernel.composition import ORCHESTRATOR_ID
from quickcode.kernel.orchestrator import resolve_orchestrator
from quickcode.kernel.preset import builtin_presets
from quickcode.kernel.resolve import resolve_composition, runtime_limits
from quickcode.subagents.definitions import builtin_defs
from quickcode.tools.registry import default_registry


@pytest.mark.parametrize("preset_id", sorted(builtin_presets()))
def test_it_is_the_spelled_out_call(tmp_path, preset_id):
    pool = list(default_registry().tools.values())
    preset = builtin_presets()[preset_id]
    defs = builtin_defs()
    by_hand = resolve_composition(
        ORCHESTRATOR_ID, pool=pool, preset=preset, defs=defs, cwd=tmp_path,
        parent=None, depth=0, max_depth=runtime_limits(tmp_path).max_depth,
        resolve_model=str.upper,
    )
    resolved = resolve_orchestrator(
        pool=pool, preset=preset, defs=defs, cwd=tmp_path, resolve_model=str.upper,
    )
    assert resolved.digest() == by_hand.digest()
    assert resolved.to_json() == by_hand.to_json()


def test_the_depth_limit_defaults_to_the_projects_setting(tmp_path):
    state_store.save_entry(tmp_path, "runtime.subagents", settings={"max_depth": 0})
    kwargs = {"pool": [], "preset": builtin_presets()["standard"],
              "defs": builtin_defs(), "cwd": tmp_path}

    # Zero levels of delegation, read off the settings file rather than the
    # dataclass default a forgotten ``max_depth=`` used to fall back to.
    assert not resolve_orchestrator(**kwargs).spawns
    # A caller resolving against a snapshot passes the limit it froze.
    assert resolve_orchestrator(**kwargs, max_depth=2).spawns
