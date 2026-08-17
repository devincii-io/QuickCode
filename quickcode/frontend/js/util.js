// Small shared helpers.

export function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function fmtTokens(n) {
  if (n == null) return "–";
  // The rounded value picks the unit, not the raw one: 999_950 is "1.0M", not
  // the nonsense "1000.0k".
  if (n >= 999950) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

export function fmtMs(ms) {
  if (ms == null) return "";
  if (ms < 1000) return ms + " ms";
  return (ms / 1000).toFixed(1) + " s";
}

export function fmtCost(usd) {
  if (!usd) return "$0.00";
  return usd < 0.1 ? "$" + usd.toFixed(4) : "$" + usd.toFixed(2);
}

export function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d) ? "" : d.toLocaleTimeString();
}

export function relTime(mtimeS) {
  const diff = Date.now() / 1000 - mtimeS;
  if (diff < 90) return "just now";
  // The unit rolls over on the rounded value, so nothing ever reads
  // "60 min ago" or "24 h ago".
  const min = Math.round(diff / 60);
  if (min < 60) return min + " min ago";
  const h = Math.round(diff / 3600);
  if (h < 24) return h + " h ago";
  return Math.round(diff / 86400) + " d ago";
}

// One-line preview of an event for dense views.
export function oneLine(s, max = 200) {
  return String(s ?? "").replace(/\s+/g, " ").trim().slice(0, max);
}

// Paint a theme (the eleven config colors) onto the CSS variables app.css
// defines. Shared by boot and by the Settings appearance picker, which applies
// a preset live before persisting it.
const THEME_VARS = {
  background: "--bg", surface: "--surface", panel: "--panel", boost: "--boost",
  foreground: "--fg", primary: "--primary", secondary: "--secondary",
  accent: "--accent", success: "--success", warning: "--warning", error: "--error",
};

export function applyTheme(theme) {
  for (const [key, cssVar] of Object.entries(THEME_VARS)) {
    if (theme?.[key]) document.documentElement.style.setProperty(cssVar, theme[key]);
  }
}

// The ghost logo ships as a separate asset (assets/icon.svg). If it is absent
// the brand must degrade to the emoji, never to a broken-image glyph.
export function wireLogo(img, className = "logo-fallback") {
  if (!img) return;
  const swap = () => {
    if (!img.isConnected) return;
    const span = document.createElement("span");
    span.className = className;
    span.textContent = "👻";
    img.replaceWith(span);
  };
  img.addEventListener("error", swap);
  // Module scripts are deferred, so the load may already have failed before
  // this ran: a finished image with no intrinsic width is exactly that.
  if (img.complete && img.naturalWidth === 0) swap();
}

// Give a non-button element a button's manners: pointer, keyboard and role.
// Used for the transcript's expandable heads and the trajectory rows, which
// cannot be <button>s without breaking their layout.
export function clickable(node, onActivate) {
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.addEventListener("click", onActivate);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onActivate(e); }
  });
  return node;
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
