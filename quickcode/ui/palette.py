"""The QuickCode color theme — warm charcoal, amber/terracotta accents.

Deliberately *not* blue, and fully user-editable: the palette is built from a
color dict (``Config.theme_colors()``) so the Theme tab in Settings can rewrite
every ``$primary``/``$accent``/``$panel`` token live and persist it. Registered
on the app and selected in ``on_mount``.
"""

from __future__ import annotations

from textual.theme import Theme

from quickcode.config import DEFAULT_THEME_COLORS

THEME_NAME = "quickcode"


def build_theme(colors: dict[str, str] | None = None) -> Theme:
    """Construct the QuickCode Textual theme from a color map.

    Missing keys fall back to the defaults, so a partial/edited map is always
    safe to pass in.
    """
    c = dict(DEFAULT_THEME_COLORS)
    if colors:
        c.update({k: v for k, v in colors.items() if v})
    return Theme(
        name=THEME_NAME,
        dark=True,
        background=c["background"],
        surface=c["surface"],
        panel=c["panel"],
        primary=c["primary"],
        secondary=c["secondary"],
        accent=c["accent"],
        foreground=c["foreground"],
        success=c["success"],
        warning=c["warning"],
        error=c["error"],
        boost=c["boost"],
        variables={
            "block-cursor-background": c["accent"],
            "block-cursor-foreground": c["background"],
            "input-selection-background": f"{c['primary']} 35%",
            "border": "#3a362b",
        },
    )


# Backwards-compatible default instance (used when no config is supplied).
QUICKCODE_THEME = build_theme()
