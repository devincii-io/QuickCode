// Settings → General, Models, Appearance.
//
// The three pages that were already here, kept working and moved out of
// modals.js so the plugin pages next door have room. Behaviour is unchanged:
// General saves the endpoint / key / default mode, Models lists the provider
// catalog read-only (the session model is switched from the composer pill),
// Appearance applies a preset live and persists it.

import { confirmModal, creditLine } from "../modals.js";
import { store } from "../store.js";
import { applyTheme, esc, fmtTokens } from "../util.js";
import { flash } from "./ui.js";

async function bootstrap(api) {
  // Settings is reachable from Home, where nothing has filled store.bootstrap —
  // fetch it on demand (unscoped, so it describes the launch project).
  if (store.bootstrap) return store.bootstrap;
  try {
    store.bootstrap = await api.bootstrap();
  } catch {
    store.bootstrap = {};
  }
  return store.bootstrap;
}

export async function renderGeneralPage(c, { api, modes }) {
  c.innerHTML = `<div class="set-loading">Loading…</div>`;
  const bs = await bootstrap(api);
  c.innerHTML = `
    <div class="set-page">
      <div class="set-lede">Where the models come from and how much the agent
        may do before it asks. These are per install and apply to new sessions.</div>
      <div class="set-field"><label>Project</label>
        <input value="${esc(bs.cwd || "")}" disabled></div>
      <div class="set-field"><label>Provider endpoint (base URL)</label>
        <input id="set-baseurl" spellcheck="false" value="${esc(bs.base_url || "")}"></div>
      <div class="set-field"><label>API key ${bs.has_api_key
        ? '<span class="ok-note">· saved</span>'
        : `<span class="warn-note">· not set (or $${esc(bs.api_key_env || "")})</span>`}</label>
        <input id="set-apikey" type="password" placeholder="sk-… (stored encrypted at rest)"></div>
      <div class="set-field"><label>Default permission mode (new sessions)</label>
        <select id="set-mode">${modes.map(([id, t]) =>
          `<option value="${id}"${bs.default_mode === id ? " selected" : ""}>${t}</option>`).join("")}
        </select></div>
      <div class="set-field"><label>Yolo mode
        <span class="qs-hint">— yolo runs every tool without asking, including
        commands that delete files and reach the network. It used to need the
        <code>--yolo</code> launch flag, which no desktop shortcut passes, so a
        permission profile asking for it was silently downgraded to “ask”.
        Arming it here only makes it <em>reachable</em>: the mode switch or a
        profile still has to select it, and a project's ceiling still caps
        it.</span></label>
        <label class="set-check"><input id="set-yolo" type="checkbox"${
          bs.allow_yolo ? " checked" : ""}>
          Allow this app to enter yolo mode</label></div>
      <div class="set-field"><label>Credits</label>
        <div class="qs-hint" id="set-credits">checking…</div></div>
      <div class="set-field"><label>Max response tokens (new sessions)
        <span class="qs-hint">— the cap sent with every request. Providers reserve
        credit against it, so a small balance is refused outright until it is
        lowered ("insufficient credits … lower max_tokens"). 0 sends no cap and
        lets the provider use its own default.</span></label>
        <input id="set-maxtok" type="number" min="0" max="200000" step="256"
               inputmode="numeric" placeholder="16384"
               value="${bs.max_tokens != null ? esc(String(bs.max_tokens)) : ""}"></div>
      <div class="set-field"><label>Temperature
        <span class="qs-hint">— blank keeps the provider's default.</span></label>
        <input id="set-temp" type="number" min="0" max="2" step="0.1"
               inputmode="decimal" placeholder="default"
               value="${bs.temperature != null ? esc(String(bs.temperature)) : ""}"></div>
      <div class="f-actions">
        <button class="btn primary" id="set-save">Save</button>
        <span class="set-flash" id="set-msg"></span>
      </div>
    </div>`;
  // Fills in on its own: a provider round trip must not hold the page.
  (async () => {
    const box = c.querySelector("#set-credits");
    if (!box) return;
    try {
      const credits = await api.credits();
      box.textContent = creditLine(credits);
      box.style.color = credits.available != null && credits.available < 1
        ? "var(--warning)" : "var(--fg-faint)";
    } catch {
      box.textContent = "could not be checked";
    }
  })();

  c.querySelector("#set-save").addEventListener("click", async () => {
    const msg = c.querySelector("#set-msg");
    try {
      const rawMax = c.querySelector("#set-maxtok").value.trim();
      const rawTemp = c.querySelector("#set-temp").value.trim();
      const patch = {
        base_url: c.querySelector("#set-baseurl").value.trim(),
        default_mode: c.querySelector("#set-mode").value,
        // Blank means "leave it as it is"; 0 means "send no cap at all".
        temperature: rawTemp === "" ? null : Number(rawTemp),
      };
      if (rawMax !== "") patch.max_tokens = Number(rawMax);

      const yolo = c.querySelector("#set-yolo");
      // Turning it ON is the one setting on this page that widens what the
      // agent may do without asking, so it is the one that asks back. Turning
      // it off needs no ceremony -- taking a capability away never surprises.
      if (yolo.checked && !bs.allow_yolo) {
        const ok = await confirmModal({
          title: "Allow yolo mode?",
          body: "In yolo mode QuickCode runs every command the model asks for "
              + "without stopping — including ones that delete files, rewrite "
              + "git history, or send data to the network. Nothing will ask "
              + "you again while a session is in that mode.",
          confirm: "Allow it",
        });
        if (!ok) { yolo.checked = false; return; }
      }
      patch.allow_yolo = yolo.checked;

      await api.putConfig(patch);
      store.bootstrap = { ...(store.bootstrap || {}), ...patch };
      const key = c.querySelector("#set-apikey").value.trim();
      if (key) await api.putApiKey(key);
      flash(msg, "Saved. New sessions pick this up.");
    } catch (err) {
      flash(msg, "Save failed: " + err.message, "err");
    }
  });
}

export async function renderModelsPage(c, { api }) {
  c.innerHTML = `
    <div class="set-page">
      <div class="set-lede">The provider's catalog. The session model is switched
        from the composer's model pill; per-agent model policy lives under
        Agents &amp; Presets.</div>
      <input id="set-model-filter" class="set-filter" spellcheck="false"
             placeholder="Filter models…" disabled>
      <div class="plug-count" id="set-model-count"></div>
      <div id="set-models" class="model-list"><div class="set-loading">Loading…</div></div>
    </div>`;
  const filter = c.querySelector("#set-model-filter");
  const count = c.querySelector("#set-model-count");
  try {
    const models = await api.models();
    const row = (mo) => `
      <div class="model-row${store.state?.model === mo.id ? " active" : ""}">
        <code class="mr-id">${esc(mo.id)}</code>
        ${store.state?.model === mo.id ? `<span class="pv-active">✓ in this session</span>` : ""}
        <span class="mr-meta">ctx ${fmtTokens(mo.context_length)}${
          mo.prompt_price != null
            ? ` · $${mo.prompt_price}/M in · $${mo.completion_price}/M out` : ""}</span>
      </div>`;
    const paint = (list) => {
      count.textContent = `${list.length} of ${models.length} models`;
      c.querySelector("#set-models").innerHTML = list.length
        ? list.map(row).join("")
        : `<div class="set-empty">No model matches that filter.</div>`;
    };
    filter.disabled = false;
    filter.addEventListener("input", () => {
      const q = filter.value.trim().toLowerCase();
      paint(models.filter((mo) => (mo.id + " " + (mo.name || "")).toLowerCase().includes(q)));
    });
    paint(models);
  } catch (err) {
    c.querySelector("#set-models").innerHTML =
      `<div class="set-error">Could not load models: ${esc(err.message)}</div>`;
  }
}

export async function renderAppearancePage(c, ctx) {
  const { api } = ctx;
  const bs = await bootstrap(api);
  const presets = bs.theme_presets || {};
  const current = bs.theme || {};
  const swatch = (colors) => ["background", "surface", "panel", "boost", "primary", "accent"]
    .map((k) => `<i style="background:${esc(colors[k] || "#000")}"></i>`).join("");
  const cards = Object.entries(presets).map(([name, colors]) => `
    <button class="theme-card" data-theme="${esc(name)}"
            ${colors.background === current.background ? 'data-current="1"' : ""}>
      <div class="tc-swatch">${swatch(colors)}</div>
      <div class="tc-name">${esc(name)}${
        colors.background === current.background ? '<span class="check">✓</span>' : ""}</div>
    </button>`).join("");
  c.innerHTML = `
    <div class="set-page">
      <div class="set-lede">Surfaces stay neutral in the dark palettes — colour
        is reserved for what it marks. Picking one applies it immediately and
        saves it.</div>
      <div class="theme-grid">${cards || "<div class='set-empty'>No presets available.</div>"}</div>
      <span class="set-flash" id="theme-msg"></span>
    </div>`;
  c.querySelector(".theme-grid")?.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-theme]");
    if (!b) return;
    const colors = presets[b.dataset.theme];
    applyTheme(colors);
    store.bootstrap = { ...(store.bootstrap || {}), theme: colors };
    try {
      await api.putConfig({ theme: colors });
      await renderAppearancePage(c, ctx);
      flash(c.querySelector("#theme-msg"), `Saved “${b.dataset.theme}”.`);
    } catch (err) {
      flash(c.querySelector("#theme-msg"), "Save failed: " + err.message, "err");
    }
  });
}
