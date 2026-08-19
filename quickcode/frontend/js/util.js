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

// Relative luminance of a #rgb / #rrggbb string, per WCAG. Anything else —
// a name, a rgb(), an empty field — returns null, and the caller leaves the
// theme flag alone rather than guessing wrong.
function luminance(hex) {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex ?? "").trim());
  if (!m) return null;
  const h = m[1].length === 3 ? m[1].replace(/./g, (c) => c + c) : m[1];
  const n = parseInt(h, 16);
  const chan = (v) => {
    v /= 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * chan((n >> 16) & 255)
       + 0.7152 * chan((n >> 8) & 255)
       + 0.0722 * chan(n & 255);
}

export function applyTheme(theme) {
  for (const [key, cssVar] of Object.entries(THEME_VARS)) {
    if (theme?.[key]) document.documentElement.style.setProperty(cssVar, theme[key]);
  }
  // The eleven colours are only half a theme: --fg-dim, the two --line weights
  // and the eight --chip-* roles are *derived*, and a percentage of light ink
  // on a dark page does not survive being asked to be dark ink on a light one.
  // css/app.css carries a second set of them under [data-theme="light"]; this
  // is the switch. It reads the background's luminance rather than the preset's
  // name because every colour is hand-editable — a user's own light palette is
  // not called "light" and still has to land on the light values. Sitting on
  // <html> rather than <body> so it also reaches :root variables and
  // color-scheme, which is what paints the native select popup.
  const lum = luminance(theme?.background);
  if (lum !== null) {
    document.documentElement.dataset.theme = lum > 0.45 ? "light" : "dark";
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
