// Install ▸ Web search — which engine the agent's `web_search` tool asks, and
// what that engine needs before it will answer.
//
// It is a page on Install rather than a section of "Provider & defaults"
// because that page is about the *model* endpoint, and two unrelated things
// called "provider" in one form is how somebody pastes a search key into the
// model field. Install is still the right neighbourhood: the choice is stored
// per install, next to the endpoint and the theme, not per project or session.
//
// Every field is drawn from the selected provider's own ProviderInfo, which
// arrives in the bootstrap payload — so SearXNG shows an instance URL and no
// key box (it is keyless), Google Programmable Search shows a key *and* an
// engine id, and the signup link and free tier are that provider's own rather
// than a generic "get an API key somewhere".
//
// A saved key never comes back from the server and is never rendered here. The
// most this page says about one is where it is being read from, in the same
// words `quickcode doctor` uses — which matters, because an environment
// variable outranks the saved key, and "I pasted a key and nothing changed"
// has to have an answer on screen.

import { store } from "../store.js";
import { esc } from "../util.js";
import { flash } from "../settings/ui.js";

const byName = (list, name) => list.find((p) => p.name === name);

// doctor's phrasing for the same state, so the two never read differently.
function stateLine(p) {
  if (!p.configured) {
    const missing = (p.missing || []).join(", and ") || "configuration";
    return `Not configured — missing ${missing}. Until that is set,
      web_search fails when the agent calls it; nothing else is affected.`;
  }
  const bits = [];
  if (p.needs_key) bits.push(`API key resolved from ${p.key_source}`);
  if (p.needs_base_url && p.base_url_in_use) bits.push(`instance ${p.base_url_in_use}`);
  for (const f of p.extra_fields || []) if (f.in_use) bits.push(`${f.key} set`);
  return `Configured — ${bits.join(", ") || "ready"}.`;
}

// Only shown once a key has resolved from somewhere other than the store: the
// box below writes the store, so in that case typing in it changes nothing.
function overrideNote(p) {
  if (!p.needs_key || !p.configured || p.key_from_store) return "";
  return `<div class="srch-note">The key in use comes from
    <code>${esc(p.key_source)}</code>, which is read before the saved one. A key
    saved here is used only once that is gone.</div>`;
}

function inUseNote(configured, resolved, envVar) {
  if (!resolved || resolved === configured) return "";
  return `<div class="srch-meta">In use: <code>${esc(resolved)}</code>${
    envVar ? ` (from <code>$${esc(envVar)}</code>)` : ""}</div>`;
}

function baseUrlField(p) {
  return `<div class="set-field">
    <label>Instance base URL</label>
    <input id="srch-base-url" spellcheck="false" autocomplete="off"
           value="${esc(p.base_url || "")}" placeholder="https://searx.example.org">
    ${inUseNote(p.base_url, p.base_url_in_use, p.base_url_env)}
    <div class="srch-meta">The SearXNG instance to query — your own, or one you
      are allowed to use. Saved in plain text in config.json${
        p.base_url_env ? `, or set <code>$${esc(p.base_url_env)}</code>` : ""}.</div>
  </div>`;
}

function keyField(p) {
  const tier = p.free_tier ? ` · free tier: ${p.free_tier}` : "";
  return `<div class="set-field">
    <label>API key ${p.configured
      ? '<span class="ok-note">· configured</span>'
      : '<span class="warn-note">· not configured</span>'}</label>
    <input id="srch-key" type="password" spellcheck="false" autocomplete="off"
           placeholder="paste the key — it is saved, never shown again">
    <div class="srch-meta">
      <a class="k-link" href="${esc(p.signup_url)}" target="_blank"
         rel="noreferrer noopener">Get a key ↗</a>${esc(tier)}${
      p.api_key_env ? ` · or set <code>$${esc(p.api_key_env)}</code>` : ""}
    </div>
    <div class="srch-meta">Saved keys go to the same encrypted store as the
      model provider key, never into config.json.</div>
  </div>`;
}

function extraField(f) {
  return `<div class="set-field">
    <label>${esc(f.key)}</label>
    <input class="srch-extra" data-key="${esc(f.key)}" spellcheck="false"
           autocomplete="off" value="${esc(f.value || "")}">
    ${inUseNote(f.value, f.in_use, f.env)}
    <div class="srch-meta">${esc(f.label)}. Not a secret: saved in config.json${
      f.env ? `, or set <code>$${esc(f.env)}</code>` : ""}.</div>
  </div>`;
}

// Keyless providers get no key box at all — offering one would be a field that
// does nothing, and the server refuses a key for them anyway.
function fieldsFor(p) {
  return [
    p.needs_base_url ? baseUrlField(p) : "",
    p.needs_key ? keyField(p) : "",
    ...(p.extra_fields || []).map(extraField),
  ].join("");
}

export async function renderWebSearchPage(c, { api }) {
  c.innerHTML = `<div class="set-page"><div class="set-loading">Reading the
    search configuration…</div></div>`;

  let search;
  try {
    // Always fresh: this page is the one that changes it, and a cached
    // "not configured" surviving a save would be the first thing anybody sees.
    const bs = await api.bootstrap();
    store.bootstrap = bs;
    search = bs.search;
  } catch (err) {
    c.innerHTML = `<div class="set-page"><div class="set-error">Could not read
      the search configuration: ${esc(err.message)}</div></div>`;
    return;
  }
  if (!search || !(search.providers || []).length) {
    c.innerHTML = `<div class="set-page"><div class="set-empty">This build has no
      search providers.</div></div>`;
    return;
  }

  const providers = search.providers;
  const inUse = byName(providers, search.provider);
  let chosen = inUse ? inUse.name : providers[0].name;

  const paint = () => {
    const p = byName(providers, chosen);
    c.innerHTML = `<div class="set-page srch-page">
      <div class="set-lede">The engine behind the agent's <code>web_search</code>
        tool. QuickCode works without one — nothing but that tool depends on it —
        and it never switches provider on its own, so whichever is selected here
        is the only one it will ask.</div>
      <div class="srch-current">Currently in use: <strong>${
        esc(inUse ? inUse.label : search.provider || "none")}</strong>${
      inUse ? "" : " — not a provider this build knows"}</div>
      <div class="set-field"><label>Search provider</label>
        <select id="srch-provider">${providers.map((o) =>
          `<option value="${esc(o.name)}"${o.name === chosen ? " selected" : ""}>${
            esc(o.label)}${o.configured ? " · configured" : ""}</option>`).join("")}
        </select></div>
      <div class="srch-state ${p.configured ? "srch-ok" : "srch-warn"}">
        <span class="srch-dot"></span>${esc(stateLine(p))}</div>
      ${overrideNote(p)}
      ${fieldsFor(p)}
      <div class="f-actions">
        <button class="btn primary" id="srch-save">Save</button>
        <span class="set-flash" id="srch-msg"></span>
      </div>
    </div>`;
    wire(p);
  };

  const wire = (p) => {
    c.querySelector("#srch-provider").addEventListener("change", (e) => {
      chosen = e.currentTarget.value;
      paint();
    });
    c.querySelector("#srch-save").addEventListener("click", () => save(p));
  };

  const save = async (p) => {
    const msg = c.querySelector("#srch-msg");
    const settings = {};
    const baseUrl = c.querySelector("#srch-base-url");
    if (baseUrl) settings.base_url = baseUrl.value.trim();
    c.querySelectorAll(".srch-extra").forEach((i) => {
      settings[i.dataset.key] = i.value.trim();
    });
    const key = c.querySelector("#srch-key")?.value.trim();
    try {
      await api.putConfig({
        search: {
          provider: p.name,
          ...(Object.keys(settings).length ? { providers: { [p.name]: settings } } : {}),
        },
      });
      // Second call by design: the key takes the encrypted route, and the
      // choice is already saved if this one fails.
      if (key) await api.putSearchKey(p.name, key);
      await renderWebSearchPage(c, { api });
      flash(c.querySelector("#srch-msg"), `Saved. web_search will ask ${p.label}.`);
    } catch (err) {
      flash(msg, "Save failed: " + err.message, "err");
    }
  };

  paint();
}
