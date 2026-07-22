"""F3 settings screen: Models / Usage / Permissions / Profile tabs."""

from __future__ import annotations

from collections.abc import Callable

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from quickcode.config import (
    DEFAULT_THEME_COLORS,
    THEME_COLOR_ORDER,
    CatalogEntry,
    Config,
    Profile,
)
from quickcode.core.agent import AgentInstance
from quickcode.providers.base import ModelInfo
from quickcode.ui.modals import format_context, format_price

_TIERS = ["quality", "balanced", "cheap"]
MODELS_PAGE_SIZE = 40


def _valid_hex(value: str) -> bool:
    """True for #rgb / #rrggbb color strings the theme builder accepts."""
    v = value.strip()
    if not v.startswith("#"):
        return False
    body = v[1:]
    return len(body) in (3, 6) and all(ch in "0123456789abcdefABCDEF" for ch in body)


class _ModelRow(Vertical):
    """One curatable model line: curated toggle, tier select, role checkboxes,
    plus a context/price info line. Reports changes via ``on_change`` so the
    curation state survives even after the row is dropped by re-filtering."""

    DEFAULT_CSS = """
    _ModelRow {
        height: auto;
        padding: 1 1 0 1;
        border-bottom: solid $panel-lighten-1;
    }

    _ModelRow.-curated {
        background: $accent 10%;
    }

    /* Container rows must be auto-height, else they default to 1fr and the
       single row balloons to fill the whole scroll area. */
    _ModelRow .row-controls,
    _ModelRow .row-meta {
        height: auto;
        width: 1fr;
    }

    _ModelRow Checkbox {
        width: auto;
        height: auto;
        border: none;
        padding: 0 1 0 0;
        background: transparent;
    }

    _ModelRow .model-id {
        width: 1fr;
        height: 3;
        content-align: left middle;
        text-style: bold;
    }

    _ModelRow Select {
        width: 15;
    }

    _ModelRow .model-row-info {
        width: 1fr;
        color: $text-muted;
        height: auto;
    }
    """

    def __init__(
        self,
        model: ModelInfo,
        entry: CatalogEntry | None,
        on_change: Callable[[str, CatalogEntry | None], None],
    ) -> None:
        super().__init__(classes="model-row -curated" if entry else "model-row")
        self.model = model
        self.model_id = model.id
        self._on_change = on_change
        self._curated = entry is not None
        self._tier = entry.tier if entry else "balanced"
        self._orch = bool(entry and "orchestrator" in entry.roles)
        self._worker = bool(entry and "worker" in entry.roles) if entry else True

    def compose(self) -> ComposeResult:
        # Line 1: curate toggle · model id (fills) · tier select
        with Horizontal(classes="row-controls"):
            yield Checkbox("use", value=self._curated, id="curate")
            yield Label(self.model_id, classes="model-id")
            yield Select(
                [(t, t) for t in _TIERS], value=self._tier, id="tier", allow_blank=False
            )
        # Line 2: role toggles · context/price info (dim)
        ctx = format_context(self.model.context_length)
        price = format_price(self.model.prompt_price, self.model.completion_price)
        with Horizontal(classes="row-meta"):
            yield Checkbox("orch", value=self._orch, id="role-orch")
            yield Checkbox("worker", value=self._worker, id="role-worker")
            yield Static(f"{ctx} ctx · {price}", classes="model-row-info")

    def to_entry(self) -> CatalogEntry | None:
        curated = self.query_one("#curate", Checkbox).value
        if not curated:
            return None
        tier = self.query_one("#tier", Select).value
        roles = []
        if self.query_one("#role-orch", Checkbox).value:
            roles.append("orchestrator")
        if self.query_one("#role-worker", Checkbox).value:
            roles.append("worker")
        if not roles:
            roles = ["worker"]
        return CatalogEntry(id=self.model_id, tier=str(tier), roles=roles)

    @on(Checkbox.Changed)
    @on(Select.Changed)
    def _changed(self) -> None:
        entry = self.to_entry()
        self.set_class(entry is not None, "-curated")
        self._on_change(self.model_id, entry)


class SettingsScreen(ModalScreen[None]):
    """F3: curate the model catalog, inspect usage/permissions, edit the profile."""

    BINDINGS = [("escape", "close", "Close"), ("f3", "close", "Close")]

    DEFAULT_CSS = """
    SettingsScreen #models-filter {
        margin: 1 0;
    }

    SettingsScreen #models-status {
        color: $text-muted;
        padding-bottom: 1;
        height: 1;
    }

    SettingsScreen #models-list {
        border: round $primary-darken-1;
        background: $boost;
        height: 1fr;
    }

    SettingsScreen .color-row {
        height: auto;
        padding: 0 0 1 0;
    }

    SettingsScreen .color-row Label {
        width: 16;
        content-align: left middle;
        height: 3;
    }

    SettingsScreen .color-row Input {
        width: 16;
    }

    SettingsScreen .color-swatch {
        width: 10;
        height: 3;
        margin-left: 1;
        border: round $panel-lighten-2;
    }

    SettingsScreen #theme-hint {
        color: $text-muted;
        padding-bottom: 1;
    }
    """

    def __init__(
        self, *, config: Config, agent: AgentInstance, app_ref=None
    ) -> None:
        super().__init__()
        self.config = config
        self.agent = agent
        self._app_ref = app_ref
        self.profile: Profile = config.profile
        # Working copy of the theme colors edited live in the Theme tab.
        self._theme_colors: dict[str, str] = config.theme_colors()
        # Curation state lives independently of the rendered (capped) page so
        # it survives filtering/re-rendering.
        self._entries: dict[str, CatalogEntry] = {e.id: e for e in self.profile.catalog}
        self._all_models: list[ModelInfo] = []
        self._models_loaded = False
        self._models_load_error = False
        self._shown = 0
        self._total = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            with TabbedContent(initial="models"):
                with TabPane("Models", id="models"):
                    yield Input(placeholder="filter models…", id="models-filter")
                    yield Static("Loading models…", id="models-status")
                    with VerticalScroll(id="models-list"):
                        pass
                    yield Button("Save catalog", id="save-models", variant="primary")
                with TabPane("Usage", id="usage"):
                    yield Static(self._usage_text(), id="usage-text")
                with TabPane("Permissions", id="permissions"):
                    yield Static(self._permissions_text(), id="permissions-text")
                with TabPane("Theme", id="theme"):
                    yield Static(
                        "Edit any color (hex, e.g. #e08a4b) — changes apply live.",
                        id="theme-hint",
                        markup=False,
                    )
                    with VerticalScroll(id="theme-list"):
                        for key in THEME_COLOR_ORDER:
                            value = self._theme_colors.get(key, "")
                            with Horizontal(classes="color-row"):
                                yield Label(key)
                                yield Input(
                                    value=value,
                                    id=f"color-{key}",
                                    classes="color-input",
                                )
                                sw = Static("", id=f"swatch-{key}", classes="color-swatch")
                                sw.styles.background = value or None
                                yield sw
                    with Horizontal(classes="key-buttons"):
                        yield Button("Save theme", id="save-theme", variant="primary")
                        yield Button("Reset to default", id="reset-theme", variant="warning")
                with TabPane("Profile", id="profile"):
                    yield Label("base_url")
                    yield Input(value=self.profile.base_url, id="profile-base-url")
                    yield Label("OpenRouter API key")
                    yield Input(
                        password=True,
                        placeholder="paste key to save (encrypted at rest)",
                        id="profile-api-key",
                    )
                    yield Static(self._key_status(), id="key-status", markup=False)
                    with Horizontal(classes="key-buttons"):
                        yield Button("Save key", id="save-key", variant="primary")
                        yield Button("Clear saved key", id="clear-key", variant="error")
                    yield Label("orchestrator_model")
                    yield Input(value=self.profile.orchestrator_model, id="profile-orch-model")
                    yield Label("worker_model")
                    yield Input(value=self.profile.worker_model, id="profile-worker-model")
                    yield Button("Save profile", id="save-profile", variant="primary")
            yield Button("Close (Esc)", id="close-settings")

    def on_mount(self) -> None:
        self._load_models()

    @work(exclusive=True)
    async def _load_models(self) -> None:
        try:
            models = await self.agent.provider.list_models()
        except Exception:
            models = []
        if not models:
            self._models_load_error = True
            ids = list(
                dict.fromkeys(
                    [e.id for e in self.profile.catalog]
                    + [self.profile.orchestrator_model, self.profile.worker_model]
                )
            )
            models = [ModelInfo(id=i, name=i) for i in ids if i]
        self._all_models = models
        self._models_loaded = True
        try:
            query = self.query_one("#models-filter", Input).value
        except Exception:
            query = ""
        self._render_models(query)

    def _sorted_default_models(self) -> list[ModelInfo]:
        """Curated ids first, then descending context length."""
        curated_ids = set(self._entries.keys())
        pinned = [m for m in self._all_models if m.id in curated_ids]
        rest = [m for m in self._all_models if m.id not in curated_ids]
        pinned.sort(key=lambda m: m.context_length or 0, reverse=True)
        rest.sort(key=lambda m: m.context_length or 0, reverse=True)
        return pinned + rest

    def _render_models(self, query: str) -> None:
        needle = query.strip().lower()
        if needle:
            matches = [m for m in self._all_models if needle in m.id.lower()]
        else:
            matches = self._sorted_default_models()

        self._total = len(matches)
        page = matches[:MODELS_PAGE_SIZE]
        self._shown = len(page)

        try:
            container = self.query_one("#models-list", VerticalScroll)
        except Exception:
            return
        container.remove_children()
        for m in page:
            container.mount(_ModelRow(m, self._entries.get(m.id), self._on_row_change))
        self._update_status()

    def _update_status(self) -> None:
        try:
            status = self.query_one("#models-status", Static)
        except Exception:
            return
        if not self._models_loaded:
            status.update("Loading models…")
            return
        note = ""
        if self._models_load_error:
            note = "  (couldn't reach provider — showing catalog/defaults)"
        status.update(
            f"showing {self._shown} of {self._total} — "
            f"{len(self._entries)} curated — type to filter{note}"
        )

    def _on_row_change(self, model_id: str, entry: CatalogEntry | None) -> None:
        if entry is None:
            self._entries.pop(model_id, None)
        else:
            self._entries[model_id] = entry
        self._update_status()

    @on(Input.Changed, "#models-filter")
    def _filter_models(self, event: Input.Changed) -> None:
        self._render_models(event.value)

    # ---- theme editor ------------------------------------------------

    @on(Input.Changed, ".color-input")
    def _color_changed(self, event: Input.Changed) -> None:
        if not event.input.id or not event.input.id.startswith("color-"):
            return
        key = event.input.id[len("color-") :]
        value = event.value.strip()
        self._theme_colors[key] = value
        # Update the swatch and re-apply the whole theme live if valid.
        if _valid_hex(value):
            try:
                self.query_one(f"#swatch-{key}", Static).styles.background = value
            except Exception:
                pass
            self._apply_theme_live()

    def _apply_theme_live(self) -> None:
        # Only push colors that parse; the app fills gaps from defaults.
        colors = {k: v for k, v in self._theme_colors.items() if _valid_hex(v)}
        if self._app_ref is not None:
            try:
                self._app_ref.apply_theme(colors)
            except Exception:
                pass

    @on(Button.Pressed, "#save-theme")
    def _save_theme(self) -> None:
        self.config.theme = {
            k: v for k, v in self._theme_colors.items() if _valid_hex(v)
        }
        self.config.save()
        self._apply_theme_live()

    @on(Button.Pressed, "#reset-theme")
    def _reset_theme(self) -> None:
        self._theme_colors = dict(DEFAULT_THEME_COLORS)
        for key, value in DEFAULT_THEME_COLORS.items():
            try:
                self.query_one(f"#color-{key}", Input).value = value
                self.query_one(f"#swatch-{key}", Static).styles.background = value
            except Exception:
                pass
        self.config.theme = dict(DEFAULT_THEME_COLORS)
        self.config.save()
        self._apply_theme_live()

    def _usage_text(self) -> str:
        ledger = self.agent.ledger
        pct = self.agent.context_pct()
        pct_s = f"{pct:.1f}%" if pct is not None else "n/a"
        return (
            f"model: {self.agent.model}\n\n"
            f"input tokens:  {ledger.input_tokens}\n"
            f"output tokens: {ledger.output_tokens}\n"
            f"cached tokens: {ledger.cached_tokens}\n"
            f"context used:  {pct_s}\n"
            f"cost (session): ${ledger.cost_usd:.4f}\n"
        )

    def _key_status(self) -> str:
        import os

        from quickcode.secrets import API_KEY_ENV, has_saved_key

        if os.environ.get(API_KEY_ENV):
            return f"Key source: {API_KEY_ENV} environment variable (takes precedence)."
        if has_saved_key():
            return "Key source: saved on disk, encrypted at rest (DPAPI on Windows)."
        return "No API key set. Paste one below and press Save key."

    def _permissions_text(self) -> str:
        eng = self.agent.permissions
        rules = eng.rules
        lines = [f"mode: {eng.mode.value}", ""]
        for label, items in (("allow", rules.allow), ("ask", rules.ask), ("deny", rules.deny)):
            lines.append(f"{label}:")
            if items:
                lines.extend(f"  - {r}" for r in items)
            else:
                lines.append("  (none)")
        return "\n".join(lines)

    @on(Button.Pressed, "#save-models")
    def _save_models(self) -> None:
        # Currently-rendered rows are the source of truth for whatever page is
        # visible right now; merge that in before falling back to the tracked
        # curation state (covers rows never touched since being rendered).
        for row in self.query_one("#models-list", VerticalScroll).children:
            if isinstance(row, _ModelRow):
                entry = row.to_entry()
                if entry is None:
                    self._entries.pop(row.model_id, None)
                else:
                    self._entries[row.model_id] = entry
        self.profile.catalog = list(self._entries.values())
        self.config.save()
        status = self.query_one("#models-status", Static)
        status.update(f"Saved {len(self._entries)} curated models.")

    @on(Button.Pressed, "#save-key")
    def _save_key(self) -> None:
        from quickcode.secrets import save_api_key

        inp = self.query_one("#profile-api-key", Input)
        key = inp.value.strip()
        if key:
            save_api_key(key)
            inp.value = ""
        self.query_one("#key-status", Static).update(self._key_status())

    @on(Button.Pressed, "#clear-key")
    def _clear_key(self) -> None:
        from quickcode.secrets import clear_saved_key

        clear_saved_key()
        self.query_one("#key-status", Static).update(self._key_status())

    @on(Button.Pressed, "#save-profile")
    def _save_profile(self) -> None:
        self.profile.base_url = self.query_one("#profile-base-url", Input).value
        self.profile.orchestrator_model = self.query_one("#profile-orch-model", Input).value
        self.profile.worker_model = self.query_one("#profile-worker-model", Input).value
        self.config.save()

    @on(Button.Pressed, "#close-settings")
    def _close_btn(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
