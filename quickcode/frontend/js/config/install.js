// Install — "where do the models come from", plus the two other things that
// belong to the install rather than to any agent: the provider catalog and the
// appearance.
//
// These three pages already existed and already worked, so they are reused
// exactly as they are (settings/general.js) rather than rewritten into the new
// shell for the sake of it. The quick-settings modal on the composer is a
// shortcut into the first of them and says so.

import { esc } from "../util.js";
import { MODES } from "../modals.js";
import {
  renderAppearancePage, renderGeneralPage, renderModelsPage,
} from "../settings/general.js";

const TABS = [
  ["general", "Provider & defaults", renderGeneralPage],
  ["models", "Model catalog", renderModelsPage],
  ["appearance", "Appearance", renderAppearancePage],
];

export function renderInstall(host, ctx, tab = "general") {
  const current = TABS.find(([id]) => id === tab) || TABS[0];
  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">Install</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="provider">»</span>
        <h2>Install</h2>
      </div>
    </header>
    <div class="cfg-lede">Per install, not per project and not per session:
      the endpoint tokens come from, the key, the mode new sessions start in,
      and how the app looks.</div>
    <div class="seg cfg-tabs">${TABS.map(([id, label]) =>
      `<a href="#/config/install/${id}" class="${id === current[0] ? "active" : ""}"
        >${esc(label)}</a>`).join("")}</div>
    <div class="cfg-install-body"></div>
  </div>`;
  const body = host.querySelector(".cfg-install-body");
  return current[2](body, { api: ctx.api, modes: MODES });
}
