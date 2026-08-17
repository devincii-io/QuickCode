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
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
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
  if (diff < 3600) return Math.round(diff / 60) + " min ago";
  if (diff < 86400) return Math.round(diff / 3600) + " h ago";
  return Math.round(diff / 86400) + " d ago";
}

// One-line preview of an event for dense views.
export function oneLine(s, max = 200) {
  return String(s ?? "").replace(/\s+/g, " ").trim().slice(0, max);
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
