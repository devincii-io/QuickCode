"""F3 settings screen: Models / Usage / Permissions / Profile tabs."""

from __future__ import annotations

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

from quickcode.config import CatalogEntry, Config, Profile
from quickcode.core.agent import AgentInstance

_TIERS = ["quality", "balanced", "cheap"]


class _ModelRow(Horizontal):
    """One curatable model line: curated toggle, tier select, role checkboxes."""

    def __init__(self, model_id: str, entry: CatalogEntry | None) -> None:
        super().__init__(classes="model-row")
        self.model_id = model_id
        self._curated = entry is not None
        self._tier = entry.tier if entry else "balanced"
        self._orch = bool(entry and "orchestrator" in entry.roles)
        self._worker = bool(entry and "worker" in entry.roles) if entry else True

    def compose(self) -> ComposeResult:
        yield Checkbox(value=self._curated, id="curate")
        yield Label(self.model_id, classes="model-id")
        yield Select(
            [(t, t) for t in _TIERS], value=self._tier, id="tier", allow_blank=False
        )
        yield Checkbox(value=self._orch, id="role-orch")
        yield Label("orch", classes="msg-reasoning")
        yield Checkbox(value=self._worker, id="role-worker")
        yield Label("worker", classes="msg-reasoning")

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


class SettingsScreen(ModalScreen[None]):
    """F3: curate the model catalog, inspect usage/permissions, edit the profile."""

    BINDINGS = [("escape", "close", "Close"), ("f3", "close", "Close")]

    def __init__(self, *, config: Config, agent: AgentInstance) -> None:
        super().__init__()
        self.config = config
        self.agent = agent
        self.profile: Profile = config.profile

    def compose(self) -> ComposeResult:
        with Vertical():
            with TabbedContent(initial="models"):
                with TabPane("Models", id="models"):
                    yield Static("Loading models…", id="models-status")
                    with VerticalScroll(id="models-list"):
                        pass
                    yield Button("Save catalog", id="save-models", variant="primary")
                with TabPane("Usage", id="usage"):
                    yield Static(self._usage_text(), id="usage-text")
                with TabPane("Permissions", id="permissions"):
                    yield Static(self._permissions_text(), id="permissions-text")
                with TabPane("Profile", id="profile"):
                    yield Label("base_url")
                    yield Input(value=self.profile.base_url, id="profile-base-url")
                    yield Label("api_key_env")
                    yield Input(value=self.profile.api_key_env, id="profile-api-key-env")
                    key_set = "set" if self.profile.api_key else "NOT SET"
                    yield Static(f"({self.profile.api_key_env} is {key_set})")
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
        ids: list[str] = []
        try:
            models = await self.agent.provider.list_models()
            ids = [m.id for m in models if m.id]
        except Exception:
            ids = []
        if not ids:
            ids = [e.id for e in self.profile.catalog]
            for default_id in (self.profile.orchestrator_model, self.profile.worker_model):
                if default_id not in ids:
                    ids.append(default_id)
        else:
            for e in self.profile.catalog:
                if e.id not in ids:
                    ids.append(e.id)

        catalog_by_id = {e.id: e for e in self.profile.catalog}
        try:
            status = self.query_one("#models-status", Static)
            status.update(f"{len(ids)} models (curated ones are checked)")
            container = self.query_one("#models-list", VerticalScroll)
            for model_id in ids:
                container.mount(_ModelRow(model_id, catalog_by_id.get(model_id)))
        except Exception:
            pass

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
        rows = list(self.query_one("#models-list", VerticalScroll).children)
        entries: list[CatalogEntry] = []
        for row in rows:
            if isinstance(row, _ModelRow):
                entry = row.to_entry()
                if entry:
                    entries.append(entry)
        self.profile.catalog = entries
        self.config.save()
        status = self.query_one("#models-status", Static)
        status.update(f"Saved {len(entries)} curated models.")

    @on(Button.Pressed, "#save-profile")
    def _save_profile(self) -> None:
        self.profile.base_url = self.query_one("#profile-base-url", Input).value
        self.profile.api_key_env = self.query_one("#profile-api-key-env", Input).value
        self.profile.orchestrator_model = self.query_one("#profile-orch-model", Input).value
        self.profile.worker_model = self.query_one("#profile-worker-model", Input).value
        self.config.save()

    @on(Button.Pressed, "#close-settings")
    def _close_btn(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
