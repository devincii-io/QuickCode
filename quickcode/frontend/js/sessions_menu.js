// The session switcher hanging off the top bar.

import { api } from "./api.js";
import { makeSelection, reportBulk } from "./selection.js";
import { openRenameSession } from "./session_rename.js";
import { store } from "./store.js";
import { menuAt } from "./ui/menu.js";
import { el, esc, oneLine, relTime } from "./util.js";

/** Arm-then-act on one button: the first click relabels it, the second runs.
 *  A `confirm()` here would block the window while the agent is streaming. */
function armButton(btn, prompt, resting) {
  if (btn.dataset.armed === "1") return true;
  btn.dataset.armed = "1";
  btn.classList.add("armed");
  btn.textContent = prompt;
  setTimeout(() => {
    if (!btn.isConnected || btn.dataset.armed !== "1") return;
    delete btn.dataset.armed;
    btn.classList.remove("armed");
    btn.textContent = resting;
  }, 4000);
  return false;
}

/** Session switcher hanging off the top bar: the sessions of the current
 *  project plus "new session", each row renamable, deletable and archivable in
 *  place. Resolves nothing — it calls back. */
export async function openSessionMenu(anchor, { onPick, onNew }) {
  let sessions = [];
  // The archive comes along on every fetch so the footer can say how much is
  // filed away before anyone asks to see it.
  try { sessions = await api.sessions(true); } catch { /* server gone */ }
  let revealed = false;
  const cur = store.convId;
  // Local to this popover: closing it is leaving the list, and a selection must
  // not outlive the list it was made in.
  const sel = makeSelection();
  let order = [];

  const m = menuAt(
    anchor,
    `<button class="menu-item" data-new><div class="mi-title">＋ New session</div>
       <div class="mi-desc">Start an empty conversation in this project.</div></button>
     <div class="menu-sep"></div><div class="menu-rows"></div>`,
    { below: true },
  );
  const rowsEl = m.querySelector(".menu-rows");
  const foot = el(`<div class="menu-foot menu-tools"></div>`);
  m.appendChild(foot);

  const row = (s) => `
    <div class="menu-row${s.archived ? " archived" : ""}${
      sel.has(s.conv_id) ? " picked" : ""}">
      <label class="mi-pick" title="Select this session (shift-click for a range)">
        <input type="checkbox" data-pick="${esc(s.conv_id)}"${
          sel.has(s.conv_id) ? " checked" : ""}></label>
      <button class="menu-item" data-conv="${esc(s.conv_id)}"
              title="${esc(oneLine(s.title, 200))}">
        <div class="mi-title"><span class="mi-name">${esc(oneLine(s.title, 90))}</span>${
          s.archived ? '<span class="mi-tag">archived</span>' : ""}${
          s.conv_id === cur ? '<span class="check">✓</span>' : ""}</div>
        <div class="mi-meta">${s.live ? "● live · " : ""}${esc(oneLine(s.model, 28))} ·
          ${s.message_count} msgs · ${relTime(s.mtime)}</div>
      </button>
      <button class="mi-act" data-rename="${esc(s.conv_id)}"
        title="Rename this session">✎</button>
      <button class="mi-act" data-arch="${esc(s.conv_id)}" data-on="${!s.archived}"
        title="${s.archived ? "Restore to the list" : "Archive: keep the file, hide the row"}"
        >${s.archived ? "⇧" : "⇩"}</button>
      <button class="mi-act mi-del" data-del="${esc(s.conv_id)}"
        title="Delete this session and its task board">✕</button>
    </div>`;

  function render() {
    const archivedCount = sessions.filter((s) => s.archived).length;
    const emptyIds = sessions
      .filter((s) => !s.archived && !s.live && !s.message_count)
      .map((s) => s.conv_id);
    const visible = sessions.filter((s) => revealed || !s.archived);
    // The rows on screen, in the order they are drawn: what a shift-click
    // ranges over and what "select all" means.
    order = visible.map((s) => s.conv_id);
    sel.keepOnly(order);
    rowsEl.innerHTML = visible.length
      ? visible.map(row).join("")
      : `<div class="menu-note">${archivedCount && !revealed
          ? "Nothing here but the archive." : "No saved sessions in this project yet."}</div>`;
    const n = sel.size;
    const allOn = order.length > 0 && n === order.length;
    foot.innerHTML = `
      ${n ? `<div class="menu-selbar">
         <span class="msb-count">${n} selected</span>
         <button class="menu-tool" data-all="${allOn ? "off" : "on"}">${
           allOn ? "select none" : `select all ${order.length}`}</button>
         <button class="menu-tool msb-del" data-bulk-del>delete ${n} session${
           n === 1 ? "" : "s"}</button>
         <span class="msb-hint">Esc clears</span>
       </div>` : ""}
      ${order.length && !n ? `<button class="menu-tool" data-all="on"
         >select all ${order.length}</button>` : ""}
      ${emptyIds.length ? `<button class="menu-tool" data-sweep
         >clean up ${emptyIds.length} empty</button>` : ""}
      ${archivedCount ? `<button class="menu-tool" data-toggle-arch
         aria-pressed="${revealed}">${revealed ? "hide" : "show"} archived
         (${archivedCount})</button>` : ""}
      <div class="menu-err"></div>`;
    // Nothing to select, nothing to sweep and nothing archived: no footer rule.
    foot.style.display = order.length || emptyIds.length || archivedCount ? "" : "none";
  }

  const fail = (err) => {
    foot.style.display = "";
    const box = foot.querySelector(".menu-err");
    if (box) box.textContent = err.message;
  };

  // Called after every write in here, so it is also where the rest of the UI
  // hears about one: the top bar's tab strip is drawn from the same list and
  // would otherwise keep offering a session that was just deleted.
  async function refresh() {
    try { sessions = await api.sessions(true); } catch (err) { fail(err); return; }
    render();
    window.dispatchEvent(new CustomEvent("qc:sessions-changed"));
  }

  render();

  // Escape clears the selection before it closes the popover. Registered now,
  // synchronously, so it runs ahead of menuAt's own Escape handler — which is
  // added on a timeout and would otherwise close the menu out from under a
  // keystroke the user meant as "never mind, deselect".
  const onSelEsc = (e) => {
    if (!m.isConnected) { document.removeEventListener("keydown", onSelEsc, true); return; }
    if (e.key !== "Escape" || !sel.size) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    sel.clear();
    render();
  };
  document.addEventListener("keydown", onSelEsc, true);

  m.addEventListener("click", async (e) => {
    if (e.target.closest("[data-new]")) { m.closeMenu(); onNew(); return; }

    const pick = e.target.closest("[data-pick]");
    if (pick) {
      // The checkbox's own state is thrown away and redrawn from the model:
      // a shift-click changes rows other than the one that was clicked.
      sel.toggle(pick.dataset.pick, order, e.shiftKey);
      render();
      return;
    }

    const all = e.target.closest("[data-all]");
    if (all) { sel.setAll(order, all.dataset.all === "on"); render(); return; }

    const bulk = e.target.closest("[data-bulk-del]");
    if (bulk) {
      const picked = sel.inOrder(order);
      const resting = `delete ${picked.length} session${picked.length === 1 ? "" : "s"}`;
      if (!armButton(bulk, `delete ${picked.length}?`, resting)) return;
      bulk.textContent = "…";
      let result;
      try {
        result = await api.removeSessions(picked);
      } catch (err) {
        // The whole request failed, so nothing was deleted: hand the button
        // back rather than leaving an ellipsis where a control used to be.
        delete bulk.dataset.armed;
        bulk.classList.remove("armed");
        bulk.textContent = resting;
        fail(err);
        return;
      }
      sel.clear();
      reportBulk(result.deleted.length, result.skipped,
        { one: "session", many: "sessions" });
      await refresh();
      return;
    }

    const toggle = e.target.closest("[data-toggle-arch]");
    if (toggle) { revealed = !revealed; render(); return; }

    const sweep = e.target.closest("[data-sweep]");
    if (sweep) {
      const n = sessions.filter((s) => !s.archived && !s.live && !s.message_count).length;
      if (!armButton(sweep, `delete ${n}?`, `clean up ${n} empty`)) return;
      sweep.textContent = "…";
      // Re-render only on success: it rewrites the footer, which is where the
      // failure would have been shown.
      try { await api.cleanupSessions(); } catch (err) { fail(err); return; }
      await refresh();
      return;
    }

    // Renaming opens a dialog, and a menu cannot stay under one, so the
    // popover closes. The name it was showing is refreshed from the top bar
    // instead — which is where the renamed session is, if it is the open one.
    const ren = e.target.closest("[data-rename]");
    if (ren) {
      const convId = ren.dataset.rename;
      const current = sessions.find((s) => s.conv_id === convId)?.title || "";
      m.closeMenu();
      openRenameSession({
        convId,
        title: current,
        save: (title) => api.renameSession(convId, title),
      });
      return;
    }

    const arch = e.target.closest("[data-arch]");
    if (arch) {
      const on = arch.dataset.on === "true";
      arch.textContent = "…";
      try {
        await api.archiveSession(arch.dataset.arch, on);
      } catch (err) {
        arch.textContent = on ? "⇩" : "⇧";
        fail(err);
        return;
      }
      await refresh();
      return;
    }

    const del = e.target.closest("[data-del]");
    if (del) {
      if (!armButton(del, "delete?", "✕")) return;
      del.textContent = "…";
      try {
        await api.removeSession(del.dataset.del);
      } catch (err) {
        delete del.dataset.armed;
        del.classList.remove("armed");
        del.textContent = "✕";
        fail(err);
        return;
      }
      await refresh();
      return;
    }

    const b = e.target.closest("[data-conv]");
    if (b) { m.closeMenu(); onPick(b.dataset.conv); }
  });
}
