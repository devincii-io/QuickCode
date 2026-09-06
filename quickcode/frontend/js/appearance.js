import { applyTheme } from "./util.js";

const KEY = "qc-appearance-v1";
export const defaults = { fontSize: 14, spacing: "comfortable", width: "focused", metrics: false, motion: true };

export function readAppearance() {
  let value;
  try { value = JSON.parse(localStorage.getItem(KEY)); } catch { /* use defaults */ }
  return {
    fontSize: Math.max(12, Math.min(20, Number(value?.fontSize) || defaults.fontSize)),
    spacing: value?.spacing === "compact" ? "compact" : defaults.spacing,
    width: value?.width === "full" ? "full" : defaults.width,
    metrics: value?.metrics === true,
    motion: value?.motion !== false,
  };
}

function apply() {
  const prefs = readAppearance();
  const root = document.documentElement;
  root.style.setProperty("--chat-size", `${prefs.fontSize}px`);
  root.style.setProperty("--content-width", prefs.width === "full" ? "none" : "860px");
  root.dataset.spacing = prefs.spacing;
  root.dataset.metrics = String(prefs.metrics);
  root.dataset.motion = String(prefs.motion);
}

export function saveAppearance(prefs) {
  localStorage.setItem(KEY, JSON.stringify(prefs));
  apply();
}

export function initAppearance() {
  apply();
  window.addEventListener("storage", (e) => {
    if (e.key === KEY) apply();
    if (e.key === "qc-theme-change") {
      try { applyTheme(JSON.parse(e.newValue).theme); } catch { /* invalid value */ }
    }
  });
}

export function renderAppearanceControls(host) {
  const p = readAppearance();
  host.innerHTML = `<div class="ws-form"><p>Saved on this device and applied to every agent pane.</p>
    <label>Conversation text size <output>${p.fontSize}px</output><input name="fontSize" type="range" min="12" max="20" value="${p.fontSize}"></label>
    <label>Spacing<select name="spacing"><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label>
    <label>Conversation width<select name="width"><option value="focused">Focused column</option><option value="full">Use the whole pane</option></select></label>
    <label class="ws-check"><input type="checkbox" name="metrics"> Show detailed token and timing metrics</label>
    <label class="ws-check"><input type="checkbox" name="motion"> Animate activity indicators</label>
    <p class="ws-error" role="alert"></p><button class="btn" data-reset>Restore defaults</button></div>`;
  host.querySelectorAll("input, select").forEach((input) => {
    if (input.type === "checkbox") input.checked = p[input.name];
    else input.value = p[input.name];
    input.addEventListener("input", () => {
      p[input.name] = input.type === "checkbox" ? input.checked : input.type === "range" ? Number(input.value) : input.value;
      host.querySelector("output").textContent = `${p.fontSize}px`;
      try { saveAppearance(p); } catch { host.querySelector(".ws-error").textContent = "Could not save appearance settings in this browser."; }
    });
  });
  host.querySelector("[data-reset]").onclick = () => {
    try { saveAppearance(defaults); renderAppearanceControls(host); }
    catch { host.querySelector(".ws-error").textContent = "Could not save appearance settings in this browser."; }
  };
}
