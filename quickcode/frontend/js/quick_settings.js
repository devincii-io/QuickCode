// Quick settings: the install-level things worth changing without leaving the
// chat. Everything else — plugins, prompt, agents, compositions — lives in the
// configuration view, and the footer link is how you get there.

import { api } from "./api.js";
import { store } from "./store.js";
import { closeModal, modal } from "./ui/modal.js";
import { applyTheme, esc } from "./util.js";

/** The balance as a sentence, including the honest "we cannot know" cases. */
export function creditLine(c) {
  if (!c || !c.supported) return c?.error || "this provider does not publish a balance";
  if (c.available == null) return c.error || "unknown";
  const money = (n) => `$${Number(n).toFixed(2)}`;
  const left = `${money(c.available)} left`;
  return c.total != null ? `${left} of ${money(c.total)}` : left;
}

export function openQuickSettings({ onFull } = {}) {
  const m = modal(
    "Quick settings",
    `<div class="qs-note">Per install, applied to new sessions. Everything else
       — tools, the prompt, agents, compositions — is in the full configuration
       view.</div>
     <div class="set-field"><label>Provider endpoint (base URL)</label>
       <input id="qs-baseurl" spellcheck="false" placeholder="loading…" disabled></div>
     <div class="set-field"><label>API key <span id="qs-key-state"></span></label>
       <input id="qs-apikey" type="password" placeholder="sk-… (stored encrypted at rest)"></div>
     <div class="set-field"><label>Credits <span id="qs-credits-state"></span></label>
       <div class="qs-hint" id="qs-credits">checking…</div></div>
     <div class="set-field"><label>Max response tokens
       <span class="qs-hint">— the provider reserves credit against this; lower it
       if you are told the balance will not cover the request. 0 = provider's own
       default.</span></label>
       <input id="qs-maxtok" type="number" min="0" max="200000" step="256"
              inputmode="numeric" placeholder="16384"></div>
     <div class="set-field"><label>Temperature
       <span class="qs-hint">— blank keeps the provider's default.</span></label>
       <input id="qs-temp" type="number" min="0" max="2" step="0.1"
              inputmode="decimal" placeholder="default"></div>
     <div class="set-field"><label>Theme</label>
       <div class="qs-themes" id="qs-themes"></div></div>
     <span class="set-flash" id="qs-msg"></span>`,
    `<button class="btn" id="qs-full">Open full configuration →</button>
     <button class="btn primary" id="qs-save">Save</button>`,
  );

  const msg = m.querySelector("#qs-msg");
  const url = m.querySelector("#qs-baseurl");
  const flashMsg = (text, kind = "ok") => {
    msg.className = `set-flash ${kind}`;
    msg.textContent = text;
  };

  (async () => {
    let bs = store.bootstrap;
    if (!bs) {
      try { bs = await api.bootstrap(); store.bootstrap = bs; } catch { bs = {}; }
    }
    if (!m.isConnected) return;
    url.disabled = false;
    url.value = bs.base_url || "";
    url.placeholder = "https://…";
    // The balance is a network round trip, so it fills in on its own rather
    // than holding the dialog closed until the provider answers.
    (async () => {
      const box = m.querySelector("#qs-credits");
      try {
        const c = await api.credits();
        if (!m.isConnected) return;
        box.textContent = creditLine(c);
        box.style.color = c.available != null && c.available < 1
          ? "var(--warning)" : "var(--fg-faint)";
      } catch {
        if (m.isConnected) box.textContent = "could not be checked";
      }
    })();
    const maxTok = m.querySelector("#qs-maxtok");
    const temp = m.querySelector("#qs-temp");
    if (bs.max_tokens != null) maxTok.value = String(bs.max_tokens);
    if (bs.temperature != null) temp.value = String(bs.temperature);
    m.querySelector("#qs-key-state").innerHTML = bs.has_api_key
      ? '<span class="ok-note">· saved</span>'
      : `<span class="warn-note">· not set (or $${esc(bs.api_key_env || "")})</span>`;
    const presets = bs.theme_presets || {};
    const current = bs.theme || {};
    m.querySelector("#qs-themes").innerHTML = Object.entries(presets).map(([name, colors]) => `
      <button class="qs-theme" data-theme="${esc(name)}"
              ${colors.background === current.background ? 'data-current="1"' : ""}
              title="${esc(name)}">
        ${["background", "panel", "primary", "accent"].map(
          (k) => `<i style="background:${esc(colors[k] || "#000")}"></i>`).join("")}
        <span>${esc(name)}</span>
      </button>`).join("");
    m.querySelector("#qs-themes").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-theme]");
      if (!b) return;
      const colors = presets[b.dataset.theme];
      applyTheme(colors);
      store.bootstrap = { ...(store.bootstrap || {}), theme: colors };
      m.querySelectorAll("[data-theme]").forEach((x) =>
        x.toggleAttribute("data-current", x === b));
      try {
        await api.putConfig({ theme: colors });
        flashMsg(`Theme “${b.dataset.theme}” saved.`);
      } catch (err) {
        flashMsg("Theme not saved: " + err.message, "err");
      }
    });
  })();

  m.querySelector("#qs-save").addEventListener("click", async () => {
    try {
      // An empty box means "leave it alone"; 0 means "send no cap".
      const rawMax = m.querySelector("#qs-maxtok").value.trim();
      const rawTemp = m.querySelector("#qs-temp").value.trim();
      const patch = { base_url: url.value.trim() };
      if (rawMax !== "") patch.max_tokens = Number(rawMax);
      patch.temperature = rawTemp === "" ? null : Number(rawTemp);
      await api.putConfig(patch);
      const key = m.querySelector("#qs-apikey").value.trim();
      if (key) await api.putApiKey(key);
      store.bootstrap = {
        ...(store.bootstrap || {}),
        base_url: url.value.trim(),
        ...(patch.max_tokens != null ? { max_tokens: patch.max_tokens } : {}),
        temperature: patch.temperature,
      };
      flashMsg("Saved. New sessions pick this up.");
    } catch (err) {
      flashMsg("Save failed: " + err.message, "err");
    }
  });
  m.querySelector("#qs-full").addEventListener("click", () => {
    closeModal();
    onFull?.();
  });
  return m;
}
