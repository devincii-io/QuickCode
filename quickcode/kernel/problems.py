"""Provenance and problems: one shape each, both flat, both closed vocabularies.

Two things every resolved value needs and neither had a home for:

``Provenance`` answers *where did this come from*. It is deliberately flat --
a chain of them (``Resolved.chain[field]``) subsumes the "overridden",
"previous layer" and "narrowed by" decorations that would otherwise accrete as
per-field extras: overridden is ``len(chain) > 1``, the previous layer is
``chain[-2].layer``, and everything else fits in ``rule`` plus one free-text
``note``.

``Problem`` answers *what did the user write that does not do what they
think*. Validation problems and resolution conflicts are the same thing at two
different times, so they are one type with one severity vocabulary: one card,
one badge, one renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Where a value came from. Seven configuration layers plus the two
# non-configuration sources (the spawning parent, and the runtime's own
# depth/limit rules).
Layer = Literal[
    "default", "user", "project", "preset", "agent",
    "session", "call", "parent", "runtime",
]

# error   = skipped or refused; a spawn carrying one must not happen.
# warning = loaded, but not what was asked for.
# info    = worth saying once; nothing was lost.
Severity = Literal["error", "warning", "info"]

LAYER_ORDER: tuple[Layer, ...] = (
    "default", "user", "project", "preset", "agent", "session", "call",
)


@dataclass(frozen=True)
class Provenance:
    layer: Layer
    # "manifest.py" | "preset:delegator" | "builtin:explore" | a file path.
    source: str = ""
    # The real path, optionally "<file>#/json/pointer".
    path: str = ""
    # The pattern or key that matched: "mcp__docs__*", "body".
    rule: str = ""
    # Set when a bundle produced the rule. Unused until bundles land.
    via_bundle: str = ""
    # The one free-text slot: "capped by cap_mode(auto-edit, ask)".
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "source": self.source,
            "path": self.path,
            "rule": self.rule,
            "via_bundle": self.via_bundle,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Provenance:
        layer = raw.get("layer", "default")
        return cls(
            layer=layer if layer in LAYER_ORDER or layer in ("parent", "runtime") else "default",
            source=str(raw.get("source", "")),
            path=str(raw.get("path", "")),
            rule=str(raw.get("rule", "")),
            via_bundle=str(raw.get("via_bundle", "")),
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class Problem:
    # Stable and machine-readable; see the vocabulary below.
    code: str
    severity: Severity
    message: str
    # The next action, imperative.
    fix: str = ""
    # The plugin id or agent id this is about.
    subject: str = ""
    # "argv", "tools", "model".
    field: str = ""
    provenance: Provenance | None = None
    # Best-effort, authored files only.
    line: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "fix": self.fix,
            "subject": self.subject,
            "field": self.field,
            "provenance": self.provenance.to_json() if self.provenance else None,
            "line": self.line,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Problem:
        prov = raw.get("provenance")
        return cls(
            code=str(raw.get("code", "")),
            severity=raw.get("severity", "warning"),
            message=str(raw.get("message", "")),
            fix=str(raw.get("fix", "")),
            subject=str(raw.get("subject", "")),
            field=str(raw.get("field", "")),
            provenance=Provenance.from_json(prov) if isinstance(prov, dict) else None,
            line=int(raw.get("line", 0) or 0),
        )


# Resolution-half vocabulary. The authoring half (bad_kind, bad_slug, ...)
# lands with the authoring validator in a later phase; the type is shared.
MODEL_NOT_SELECTABLE = "model_not_selectable"
MODEL_OUTSIDE_SET = "model_outside_set"
TOOL_WITHHELD_BY_PARENT = "tool_withheld_by_parent"
SPAWN_WITHHELD_BY_PARENT = "spawn_withheld_by_parent"
UNKNOWN_AGENT = "unknown_agent"
PATTERN_MATCHED_NOTHING = "pattern_matched_nothing"
TOOL_NOT_INSTALLED = "tool_not_installed"
UNKNOWN_SECTION = "unknown_section"
CEILING_CAPPED = "ceiling_capped"
UNKNOWN_AGENT_REF = "unknown_agent_ref"
BAD_COMPOSITION = "bad_composition"
LOCAL_SETTINGS_IGNORED = "local_settings_ignored"
ID_RESERVED = "id_reserved"


def worst(problems: list[Problem]) -> Severity:
    for level in ("error", "warning", "info"):
        if any(p.severity == level for p in problems):
            return level  # type: ignore[return-value]
    return "info"
