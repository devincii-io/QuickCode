// Telling you when an agent you are not looking at needs you.
//
// A pane watches its own conversation (`turnWatcher`) and says when a turn
// finished, when a permission or plan review is waiting, and when a turn
// failed. Whoever can tell whether you are looking keeps the unseen count
// (`Attention`): the workspace shell for its panes, told over the validated
// message bridge, or a standalone pane for itself. From that count come the
// badges, the "(2) QuickCode" window title and — only when you switched them
// on, only while the window is in the background — desktop notifications.
// None of it makes a sound.

export const NOTICE_KINDS = ["review", "error", "done"];    // most urgent first
const LABEL = { review: "needs approval", error: "stopped with an error", done: "finished" };

/** A function fed every store notification; returns a notice or null.
 *  Replayed history is never news, and a turn you interrupted yourself is not
 *  announced when it ends. */
export function turnWatcher() {
  let busy = false, failed = false, stopped = false;
  return (kind, ev, replaying = false) => {
    if (kind === "reset") { busy = failed = stopped = false; return null; }
    if (kind === "state") {
      const was = busy;
      busy = !!ev?.busy;
      if (busy === was) return null;
      const out = busy || stopped ? null : { kind: failed ? "error" : "done" };
      failed = stopped = false;
      return out;
    }
    if (kind === "status") {
      if (ev?.state === "error") failed = true;
      if (ev?.state === "interrupted") stopped = true;
      return null;
    }
    if (kind !== "event" || replaying) return null;
    if (ev.type === "permission_request") return { kind: "review", detail: String(ev.tool || "") };
    if (ev.type === "plan_request") return { kind: "review", detail: "plan" };
    if (ev.type === "error") {
      if (busy) { failed = true; return null; }
      return { kind: "error" };
    }
    return null;
  };
}

/** Unseen notices per pane id. */
export class Attention {
  constructor() { this.byId = new Map(); }

  add(id, kind) {
    if (!NOTICE_KINDS.includes(kind)) return false;
    const entry = this.byId.get(id) || { count: 0, kind };
    entry.count += 1;
    if (NOTICE_KINDS.indexOf(kind) < NOTICE_KINDS.indexOf(entry.kind)) entry.kind = kind;
    this.byId.set(id, entry);
    return true;
  }

  clear(id) { return this.byId.delete(id); }

  get(id) { return this.byId.get(id) || null; }

  /** The combined entry for several panes: a workspace in the sidebar. */
  sum(ids) {
    let out = null;
    for (const id of ids) {
      const e = this.byId.get(id);
      if (!e) continue;
      if (!out) out = { count: 0, kind: e.kind };
      out.count += e.count;
      if (NOTICE_KINDS.indexOf(e.kind) < NOTICE_KINDS.indexOf(out.kind)) out.kind = e.kind;
    }
    return out;
  }

  total() {
    let n = 0;
    for (const e of this.byId.values()) n += e.count;
    return n;
  }

  /** Forget panes that are gone. */
  retain(ids) {
    for (const id of [...this.byId.keys()]) if (!ids.has(id)) this.byId.delete(id);
  }
}

export function badgeTitle(title, count) {
  const base = String(title ?? "").replace(/^\(\d+\+?\) /, "");
  return count > 0 ? `(${count > 99 ? "99+" : count}) ${base}` : base;
}

export function describe(entry) {
  if (!entry) return "";
  return `${entry.count} unseen — ${LABEL[entry.kind]}`;
}

/** What a desktop notification says. Nothing from the conversation itself —
 *  an error message or a command can hold what a lock screen should not show. */
export function noticeCopy(kind, who, detail = "") {
  const name = who || "An agent";
  if (kind === "review") {
    const body = detail === "plan" ? "A plan is waiting for your review."
      : detail ? `Waiting for permission to use ${detail}.` : "A permission prompt is waiting.";
    return { title: `${name} needs approval`, body };
  }
  if (kind === "error") return { title: `${name} stopped with an error`, body: "Open it to see what went wrong." };
  return { title: `${name} finished`, body: "The agent's turn is over." };
}

/** Whether you can see this document right now. */
export function windowActive() {
  return document.visibilityState === "visible" && document.hasFocus();
}

/** "unsupported" where the window has no Notification API (the native
 *  window's WebView may not), else the browser's permission. */
export function notifySupport() {
  return typeof Notification === "function" ? Notification.permission : "unsupported";
}

/** Ask for permission. Only ever call this from a user gesture. */
export async function requestNotify() {
  if (notifySupport() === "unsupported") return "unsupported";
  try { return await Notification.requestPermission(); } catch { return "denied"; }
}

/** A silent desktop notification, or nothing at all where there cannot be one. */
export function showOsNotice({ title, body }, { tag, onClick } = {}) {
  if (notifySupport() !== "granted") return null;
  try {
    const n = new Notification(title, { body, tag, silent: true, icon: "assets/icon.svg" });
    n.onclick = () => { n.close(); onClick?.(); };
    return n;
  } catch {
    return null;   // some embedders expose the constructor and refuse to use it
  }
}

/** The count pill beside a name. Created on first use, removed when seen. */
export function paintBadge(host, entry, before = null) {
  let pill = host.querySelector(":scope > .ws-notice");
  if (!entry) { pill?.remove(); return; }
  if (!pill) {
    pill = document.createElement("span");
    pill.className = "ws-notice";
    host.insertBefore(pill, before);
  }
  const text = entry.count > 99 ? "99+" : String(entry.count);
  if (pill.textContent !== text) pill.textContent = text;
  pill.dataset.kind = entry.kind;
  pill.title = describe(entry);
  pill.setAttribute("aria-label", describe(entry));
}
