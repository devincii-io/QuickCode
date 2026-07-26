"""Context-engineering (identity + per-turn mode reminder) and theming."""

from quickcode.config import DEFAULT_THEME_COLORS, THEME_PRESETS, Config, Environment
from quickcode.prompts.system import mode_reminder, render_system_prompt
from quickcode.ui.palette import THEME_NAME, build_theme
from quickcode.ui.statusbar import StatusBar


def test_identity_names_the_running_model():
    env = Environment.detect()
    prompt = render_system_prompt(env, model="anthropic/claude-opus-4.8", provider="OpenRouter")
    assert "anthropic/claude-opus-4.8" in prompt
    assert "OpenRouter" in prompt
    # The model line lives in the (cache-stable) identity section.
    assert prompt.index("claude-opus-4.8") < prompt.index("<environment>")


def test_mode_reminder_covers_every_mode():
    for value in ("plan", "ask", "auto-edit", "dontask", "yolo"):
        assert mode_reminder(value)
    assert mode_reminder("nonsense") == ""


def test_build_theme_is_not_blue_and_uses_overrides():
    theme = build_theme(Config().theme_colors())
    assert theme.name == THEME_NAME
    # Default accent is warm amber/terracotta, not a blue.
    assert theme.accent == DEFAULT_THEME_COLORS["accent"]
    # User overrides win.
    custom = build_theme({"accent": "#00ff00"})
    assert custom.accent == "#00ff00"
    # A partial/edited map still fills the rest from defaults.
    assert custom.primary == DEFAULT_THEME_COLORS["primary"]


def test_theme_colors_persist_round_trip(tmp_path):
    cfg = Config()
    cfg.theme = {"accent": "#abcdef"}
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.theme_colors()["accent"] == "#abcdef"
    # Untouched keys fall back to defaults.
    assert loaded.theme_colors()["primary"] == DEFAULT_THEME_COLORS["primary"]


def test_theme_presets_are_complete_and_light_mode_is_detected():
    for colors in THEME_PRESETS.values():
        assert set(colors) == set(DEFAULT_THEME_COLORS)
    assert build_theme(THEME_PRESETS["dark"]).dark is True
    assert build_theme(THEME_PRESETS["light"]).dark is False


def test_tiny_nonzero_context_is_not_displayed_as_zero():
    status = StatusBar()
    status.ctx_pct = 0.25
    assert "<1%" in status._ctx_text()
