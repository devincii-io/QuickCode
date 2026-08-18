// The generated form: one control per SettingSpec, and the tier rules that
// govern writing it.
//
// free    — the control saves and nothing asks.
// confirm — the control saves, the server answers 409 with the *reason*, and
//           that reason is what the dialog puts in front of the user. Only
//           then does the same write go again with confirmed: true.
// locked  — the control is rendered read-only with its value in full and a
//           line saying why. Never hidden, never a mystery grey box.

import { el, esc } from "../util.js";
import { confirmRisk, flash, splitError, tierBadge } from "./ui.js";

const LOCKED_NOTE =
  "Part of how QuickCode works — this one is fixed. You can read it here in "
  + "full, and the plugin's raw definition shows where it comes from.";

function toText(setting, value) {
  if (Array.isArray(value)) return value.join("\n");
  if (value === null || value === undefined) return "";
  return String(value);
}

function controlHtml(plugin, s) {
  const locked = s.tier === "locked";
  const id = `f-${plugin.id}-${s.key}`.replace(/[^\w-]/g, "_");
  const dis = locked ? " disabled" : "";
  const ro = locked ? " readonly" : "";
  const v = s.value;
  switch (s.type) {
    case "bool":
      return `<label class="f-switch${locked ? " is-locked" : ""}">
        <input type="checkbox" id="${id}" data-ctl${v ? " checked" : ""}${dis}>
        <span class="f-track"><span class="f-knob"></span></span>
        <span class="f-switch-text">${v ? "On" : "Off"}</span></label>`;
    case "enum":
      return `<select id="${id}" data-ctl${dis}>${
        (s.choices || []).map((c) =>
          `<option value="${esc(c)}"${c === v ? " selected" : ""}>${esc(c)}</option>`).join("")
      }</select>`;
    case "int":
    case "float":
      return `<div class="f-num">
        <input type="number" id="${id}" data-ctl value="${esc(toText(s, v))}"
               step="${s.type === "int" ? "1" : "0.01"}"
               ${s.minimum != null ? `min="${esc(s.minimum)}"` : ""}
               ${s.maximum != null ? `max="${esc(s.maximum)}"` : ""}${dis}${ro}>
        ${s.minimum != null || s.maximum != null
          ? `<span class="f-range">${s.minimum ?? "–"} … ${s.maximum ?? "–"}</span>` : ""}
      </div>`;
    case "text":
      return `<textarea id="${id}" data-ctl class="f-text" rows="10"
        spellcheck="false"${dis}${ro}>${esc(toText(s, v))}</textarea>`;
    case "list":
      return `<textarea id="${id}" data-ctl class="f-text f-list" rows="4"
        spellcheck="false" placeholder="one per line"${dis}${ro}>${esc(toText(s, v))}</textarea>`;
    default:
      return `<input type="text" id="${id}" data-ctl spellcheck="false"
        value="${esc(toText(s, v))}"${dis}${ro}>`;
  }
}

function fieldHtml(plugin, s) {
  const locked = s.tier === "locked";
  const instant = s.type === "bool" || s.type === "enum";
  return `<div class="set-f${locked ? " locked" : ""}" data-key="${esc(s.key)}"
       data-type="${esc(s.type)}" data-tier="${esc(s.tier)}">
    <div class="f-head">
      <label class="f-label">${esc(s.title || s.key)}</label>
      ${tierBadge(s.tier)}
      <code class="f-key">${esc(s.key)}</code>
    </div>
    ${s.help ? `<div class="f-help">${esc(s.help)}</div>` : ""}
    ${s.tier === "confirm" && s.risk ? `<div class="f-risk">${esc(s.risk)}</div>` : ""}
    <div class="f-ctl">${controlHtml(plugin, s)}</div>
    ${locked
      ? `<div class="f-locked-note">${esc(LOCKED_NOTE)}</div>`
      : `<div class="f-actions${instant ? " instant" : ""}">
           ${instant ? "" : `<button class="btn f-save" data-save disabled>Save</button>
           <button class="btn f-revert" data-revert disabled>Revert</button>`}
           <span class="set-flash" data-flash></span>
         </div>`}
  </div>`;
}

/** Build the settings form for one plugin. `onUpdated(plugin)` fires with the
 *  server's fresh copy after every accepted write. */
export function renderSettingsForm(plugin, { api, onUpdated } = {}) {
  const node = el(`<div class="set-form">${
    plugin.settings.length
      ? plugin.settings.map((s) => fieldHtml(plugin, s)).join("")
      : `<div class="set-empty">No settings — this plugin is either on or it
           is not. Use View to read what it does.</div>`
  }</div>`);

  // The last value the server acknowledged, per key: what Revert goes back to
  // and what a cancelled confirm dialog restores.
  const saved = new Map(plugin.settings.map((s) => [s.key, s.value]));

  for (const s of plugin.settings) {
    if (s.tier === "locked") continue;
    const f = node.querySelector(`.set-f[data-key="${CSS.escape(s.key)}"]`);
    const ctl = f.querySelector("[data-ctl]");
    const saveBtn = f.querySelector("[data-save]");
    const revertBtn = f.querySelector("[data-revert]");
    const msg = f.querySelector("[data-flash]");
    const instant = s.type === "bool" || s.type === "enum";

    const read = () => {
      if (s.type === "bool") return ctl.checked;
      if (s.type === "int") return parseInt(ctl.value, 10);
      if (s.type === "float") return parseFloat(ctl.value);
      if (s.type === "list") return ctl.value.split("\n").map((x) => x.trim()).filter(Boolean);
      return ctl.value;
    };
    const paint = (value) => {
      if (s.type === "bool") {
        ctl.checked = !!value;
        f.querySelector(".f-switch-text").textContent = value ? "On" : "Off";
      } else if (s.type === "list") {
        ctl.value = Array.isArray(value) ? value.join("\n") : String(value ?? "");
      } else {
        ctl.value = value === null || value === undefined ? "" : String(value);
      }
    };
    const dirty = () => {
      if (instant) return false;
      const now = read();
      const was = saved.get(s.key);
      if (Array.isArray(was)) return JSON.stringify(now) !== JSON.stringify(was);
      if (typeof was === "number") return Number(now) !== was;
      return String(now ?? "") !== String(was ?? "");
    };
    const syncButtons = () => {
      if (instant) return;
      const d = dirty();
      saveBtn.disabled = !d;
      revertBtn.disabled = !d;
      f.classList.toggle("dirty", d);
    };

    const commit = async () => {
      const value = read();
      if (s.type === "int" || s.type === "float") {
        if (Number.isNaN(value)) { flash(msg, "Needs a number.", "err"); return; }
      }
      const send = (confirmed) =>
        api.updatePlugin(plugin.id, { settings: { [s.key]: value }, confirmed });
      try {
        let fresh;
        try {
          fresh = await send(false);
        } catch (err) {
          const { status, detail } = splitError(err);
          if (status !== 409) throw err;
          // The server's 409 detail *is* the risk. Show that, not a generic
          // "are you sure?".
          const ok = await confirmRisk({
            title: `${plugin.title} · ${s.title || s.key}`,
            what: `<code class="cf-code">${esc(s.key)}</code> →
                   <code class="cf-code">${esc(toText(s, value)).slice(0, 400)}</code>`,
            reason: detail,
          });
          if (!ok) { paint(saved.get(s.key)); syncButtons(); flash(msg, "Left unchanged."); return; }
          fresh = await send(true);
        }
        const updated = (fresh.settings || []).find((x) => x.key === s.key);
        saved.set(s.key, updated ? updated.value : value);
        paint(saved.get(s.key));
        syncButtons();
        flash(msg, "Saved.");
        onUpdated?.(fresh);
      } catch (err) {
        const { status, detail } = splitError(err);
        paint(saved.get(s.key));
        syncButtons();
        flash(msg, status === 403 ? `Refused — ${detail}` : detail, "err");
      }
    };

    if (instant) {
      ctl.addEventListener("change", () => {
        if (s.type === "bool") {
          f.querySelector(".f-switch-text").textContent = ctl.checked ? "On" : "Off";
        }
        commit();
      });
    } else {
      ctl.addEventListener("input", syncButtons);
      ctl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && s.type !== "text" && s.type !== "list") {
          e.preventDefault();
          commit();
        }
      });
      saveBtn.addEventListener("click", commit);
      revertBtn.addEventListener("click", () => { paint(saved.get(s.key)); syncButtons(); });
    }
  }

  return node;
}
