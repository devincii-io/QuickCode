import { api, authToken, initAuth } from "./api.js";
import { initHome, refreshHome, rememberProject } from "./home.js";
import { openDirBrowser } from "./dirbrowser.js";
import { applyTheme, el, esc } from "./util.js";
import { toastError, toastOk } from "./toast.js";
import { readAppearance, renderAppearanceControls } from "./appearance.js";
import { Attention, badgeTitle, noticeCopy, paintBadge, showOsNotice, windowActive } from "./notify.js";
import { dwindleDir, equalize, evenRatio, heirOf, insertBeside, layoutRects, leaves, removeLeaf } from "./split_tree.js";
import { MAX_PANES, MAX_RATIO, MIN_RATIO, clampRatio, resizeKey, restoreWorkspace } from "./workspace_state.js";

const KEY = "qc-workspaces-v1";
const GAP = 6;
const workspaces = new Map();
const frames = new Map();
// Keyed by split node so a re-layout moves a divider instead of replacing it,
// which would drop its keyboard focus and any pointer capture on it.
const dividers = new Map();
let active = null, zoomed = null, home = true, shell, grid, sidebar, utility;
let dragged = null, undo = null, storageWarning = false;
let chrome = { width: 232, collapsed: false };
// Turns finished, reviews waiting and errors in panes you were not looking at.
const attention = new Attention();

function saveChrome() {
  try { localStorage.setItem("qc-workspace-chrome", JSON.stringify(chrome)); } catch { /* current window keeps its layout */ }
}
function showSidebar() {
  shell.classList.toggle("ws-collapsed", chrome.collapsed);
  document.getElementById("ws-sidebar-toggle").setAttribute("aria-expanded", String(!chrome.collapsed));
}
function toggleSidebar() {
  chrome.collapsed = !chrome.collapsed;
  showSidebar();
  saveChrome();
}

function initSidebarResize() {
  try {
    const saved = JSON.parse(localStorage.getItem("qc-workspace-chrome"));
    if (saved) chrome = { width: Math.max(160, Math.min(360, Number(saved.width) || 232)), collapsed: saved.collapsed === true };
  } catch { /* defaults */ }
  showSidebar();
  const grip = shell.querySelector(".ws-sidebar-grip");
  const apply = (width) => {
    chrome.width = Math.max(160, Math.min(360, width));
    shell.style.setProperty("--sidebar-width", `${chrome.width}px`);
    grip.setAttribute("aria-valuenow", String(Math.round(chrome.width)));
  };
  apply(chrome.width);
  grip.addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const width = { ArrowLeft: chrome.width - 10, ArrowRight: chrome.width + 10, Home: 160, End: 360 }[e.key];
    if (width === undefined) return;
    e.preventDefault(); apply(width); saveChrome();
  });
  grip.addEventListener("dblclick", () => { apply(232); saveChrome(); });
  grip.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); grip.setPointerCapture(e.pointerId);
    grid.classList.add("ws-resizing");
    const move = (ev) => apply(ev.clientX - shell.getBoundingClientRect().left);
    const end = () => {
      grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", end); grip.removeEventListener("pointercancel", end);
      grid.classList.remove("ws-resizing");
      if (grip.hasPointerCapture(e.pointerId)) grip.releasePointerCapture(e.pointerId);
      saveChrome();
    };
    grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", end); grip.addEventListener("pointercancel", end);
  });
}

function save() {
  try {
    localStorage.setItem(KEY, JSON.stringify({ version: 1, active,
      workspaces: [...workspaces.values()].map(({ project, tree, panes, focused, name }) => ({ project, tree, focused, name,
        panes: Object.fromEntries(Object.entries(panes).map(([id, p]) => [id, {
          id, title: p.title, convId: p.persisted ? p.convId : null,
        }])) })) }));
  } catch {
    if (!storageWarning) toastError("This browser could not save the workspace layout. Keep this window open to retain it.");
    storageWarning = true;
  }
}

function current() { return workspaces.get(active); }
function post(frame, data) { frame?.contentWindow?.postMessage({ source: "qc-workspace", ...data }, location.origin); }

function showHome() {
  home = true;
  document.getElementById("app").classList.add("showing-home");
  shell.classList.add("ws-home");
  render();
  refreshHome();
}

async function activate(ws) {
  const pid = ws.project.id;
  active = pid;
  home = false;
  zoomed = null;
  shell.classList.remove("ws-home");
  document.getElementById("app").classList.remove("showing-home");
  render();
  try {
    // Restored projects may not have a server manager until reopened.
    if (!ws.ready) {
      ws.opening ||= api.openProject(ws.project.path);
      ws.project = { ...ws.project, ...await ws.opening };
      ws.ready = true;
    }
    if (active !== pid || home) return;
    if (!leaves(ws.tree).length) addPane(ws);
    else { for (const id of leaves(ws.tree)) mount(ws, ws.panes[id]); }
    rememberProject(pid);
    render();
    save();
    acknowledge();
  } catch (err) {
    ws.opening = null;
    toastError(`Could not open ${ws.project.name || ws.project.path}: ${err.message}`);
    if (active === pid) showHome();
  }
}

async function openProject(project, { resume = null } = {}) {
  let ws = workspaces.get(project.id);
  if (!ws) {
    ws = { project, tree: null, panes: {}, focused: null, ready: true };
    workspaces.set(project.id, ws);
    if (resume) addPane(ws, null, resume);
  }
  await activate(ws);
  if (resume) addPane(ws, null, resume);
}

// `seq` is an event to open in the pane's inspector: a session search hit.
function addPane(ws = current(), dir = null, convId = null, seq = null) {
  if (!ws) return;
  const at = Number.isSafeInteger(seq) ? seq : null;
  const existing = convId && Object.values(ws.panes).find((p) => p.convId === convId);
  if (existing) {
    focus(ws, existing.id);
    if (at !== null) post(frames.get(existing.id)?.iframe, { action: "reveal", seq: at });
    return;
  }
  if (leaves(ws.tree).length >= MAX_PANES) {
    toastError(`A workspace can show up to ${MAX_PANES} agents. Close a pane before adding another.`);
    return;
  }
  const id = crypto.randomUUID();
  const pane = { id, convId, title: "New agent", persisted: !!convId, reveal: at };
  const box = frames.get(ws.focused)?.element.getBoundingClientRect() || grid.getBoundingClientRect();
  ws.tree = insertBeside(ws.tree, ws.focused, id, dir || dwindleDir(box));
  ws.panes[id] = pane;
  ws.focused = id;
  zoomed = null;
  mount(ws, pane);
  render();
  save();
}

function focus(ws, id, input = true) {
  if (active !== ws.project.id || home) activate(ws);
  ws.focused = id;
  if (zoomed && zoomed !== id) zoomed = null;
  attention.clear(id);
  render();
  save();
  if (input) post(frames.get(id)?.iframe, { action: "focus" });
}

// Whether you can see this pane now: its notices are read the moment you can.
function watching(f) {
  return windowActive() && !home && !utility && active === f.ws.project.id
    && f.ws.focused === f.pane.id && (!zoomed || zoomed === f.pane.id);
}

function acknowledge() {
  const f = frames.get(current()?.focused);
  if (f && watching(f) && attention.clear(f.pane.id)) render();
}

function notice(f, data) {
  if (watching(f) || !attention.add(f.pane.id, data.kind)) return;
  const copy = noticeCopy(data.kind, f.pane.title, typeof data.detail === "string" ? data.detail.slice(0, 60) : "");
  document.getElementById("ws-live").textContent = `${copy.title}. ${copy.body}`;
  render();
  if (!windowActive() && readAppearance().notify) {
    showOsNotice(copy, { tag: `qc-${f.pane.id}`, onClick: () => {
      window.focus();
      if (frames.get(f.pane.id) === f) focus(f.ws, f.pane.id);
    } });
  }
}

function closePane(ws, id) {
  const pane = ws.panes[id];
  if (!pane) return;
  undo = { ws, pane };
  const heir = heirOf(ws.tree, id);
  ws.tree = removeLeaf(ws.tree, id);
  delete ws.panes[id];
  frames.get(id)?.element.remove();
  frames.delete(id);
  const wasFocused = ws.focused === id;
  if (wasFocused) ws.focused = heir || leaves(ws.tree)[0] || null;
  zoomed = null;
  render();
  save();
  // The close button left with its pane. Keyboard focus goes where the room went.
  if (wasFocused && ws.focused) post(frames.get(ws.focused)?.iframe, { action: "focus" });
  else if (wasFocused) document.querySelector("#ws-empty button")?.focus();
  toastOk("Pane closed. The conversation is kept in session history.");
}

function undoClose() {
  if (!undo) return;
  const { ws, pane } = undo;
  // Reopened from history since it closed: one pane per conversation.
  const open = pane.convId && Object.values(ws.panes).find((p) => p.convId === pane.convId);
  if (open) { undo = null; focus(ws, open.id); return; }
  if (leaves(ws.tree).length >= MAX_PANES) { toastError("Close a pane before restoring another."); return; }
  undo = null;
  ws.panes[pane.id] = pane;
  ws.tree = insertBeside(ws.tree, ws.focused, pane.id);
  ws.focused = pane.id;
  mount(ws, pane);
  activate(ws);
}

function mount(ws, pane) {
  if (frames.has(pane.id)) return;
  const element = el(`<section class="ws-pane" aria-label="Agent: ${esc(pane.title)}">
    <header class="ws-pane-head" draggable="true">
      <span class="ws-dot" aria-hidden="true"></span><button class="ws-pane-name" title="Rename conversation">${esc(pane.title)}</button>
      <span class="ws-pane-status">Connecting</span>
      <button class="ws-icon" data-action="h" title="Split right" aria-label="Split right">◫</button>
      <button class="ws-icon" data-action="v" title="Split below" aria-label="Split below">⬒</button>
      <button class="ws-icon" data-action="zoom" title="Maximize pane (Alt+Z)" aria-label="Maximize pane">⛶</button>
      <button class="ws-icon" data-action="close" title="Close pane; agent keeps running" aria-label="Close pane">×</button>
    </header><iframe title="${esc(pane.title)}" allow="clipboard-write"></iframe>
  </section>`);
  const iframe = element.querySelector("iframe");
  const query = new URLSearchParams({ pane: "1", project: ws.project.id, view: pane.id });
  if (pane.convId) query.set("resume", pane.convId);
  if (pane.convId && pane.reveal != null) query.set("at", String(pane.reveal));
  delete pane.reveal;
  // sessionStorage is shared by same-origin frames in this tab. No secret is
  // put in a frame URL, a saved layout, or a postMessage payload.
  iframe.src = `${location.pathname}?${query}`;
  element.dataset.pane = pane.id;
  element.id = `ws-pane-${pane.id}`;
  frames.set(pane.id, { element, iframe, ws, pane });
  grid.appendChild(element); // Never reparent a mounted iframe: it would reload.
  const select = () => { if (home || active !== ws.project.id || ws.focused !== pane.id) focus(ws, pane.id, false); };
  element.addEventListener("pointerdown", select);
  element.addEventListener("focusin", select);
  element.querySelector(".ws-pane-head").addEventListener("dblclick", (e) => {
    if (!e.target.closest("button")) toggleZoom(pane.id);
  });
  element.querySelector(".ws-pane-name").onclick = () => renamePane(ws, pane);
  element.querySelectorAll("[data-action]").forEach((button) => {
    button.onclick = () => {
      const action = button.dataset.action;
      if (action === "close") closePane(ws, pane.id);
      else if (action === "zoom") toggleZoom(pane.id);
      else { ws.focused = pane.id; addPane(ws, action); } // Keyboard activation has no pointerdown.
    };
  });
  element.addEventListener("dragstart", (e) => {
    dragged = pane.id;
    e.dataTransfer.setData("text/plain", pane.id);
    e.dataTransfer.effectAllowed = "move";
    grid.classList.add("ws-moving");
  });
  element.addEventListener("dragend", clearDrag);
  element.addEventListener("dragover", (e) => {
    if (!dragged || dragged === pane.id) return;
    e.preventDefault();
    const side = dropSide(element, e);
    element.dataset.drop = side;
  });
  element.addEventListener("dragleave", (e) => { if (!element.contains(e.relatedTarget)) delete element.dataset.drop; });
  element.addEventListener("drop", (e) => {
    e.preventDefault();
    if (!dragged || dragged === pane.id || !ws.panes[dragged]) { clearDrag(); return; }
    const side = dropSide(element, e);
    ws.tree = removeLeaf(ws.tree, dragged);
    ws.tree = insertBeside(ws.tree, pane.id, dragged, ["left", "right"].includes(side) ? "h" : "v",
      { first: ["left", "top"].includes(side) });
    clearDrag(); render(); save();
  });
}

function dropSide(element, e) {
  const r = element.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
  const distances = [x, 1 - x, y, 1 - y];
  return ["left", "right", "top", "bottom"][distances.indexOf(Math.min(...distances))];
}
function clearDrag() {
  dragged = null; grid.classList.remove("ws-moving");
  for (const { element } of frames.values()) delete element.dataset.drop;
}

function toggleZoom(id = current()?.focused) {
  if (!id) return;
  zoomed = zoomed === id ? null : id;
  render();
}

function box(element, rect) {
  for (const key of ["left", "top", "width", "height"]) element.style[key] = `${rect[key]}px`;
}

function makeDivider(node) {
  const element = el(`<div class="ws-divider" role="separator" tabindex="0"
    aria-valuemin="${Math.round(MIN_RATIO * 100)}" aria-valuemax="${Math.round(MAX_RATIO * 100)}"></div>`);
  const resize = (ratio) => { node.ratio = ratio; layout(); };
  element.addEventListener("keydown", (e) => {
    const ratio = resizeKey(node.ratio, e);
    if (ratio === null) return;
    e.preventDefault(); resize(ratio); save();
  });
  element.addEventListener("dblclick", () => { resize(evenRatio(node)); save(); });
  element.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    // Captured on the divider itself, so the dblclick that follows lands on it.
    element.setPointerCapture(e.pointerId);
    grid.classList.add("ws-resizing");
    const move = (ev) => {
      const split = dividers.get(node)?.box;
      if (!split) return;
      const bounds = grid.getBoundingClientRect();
      const horizontal = node.dir !== "v";
      const offset = horizontal ? ev.clientX - bounds.left - split.left : ev.clientY - bounds.top - split.top;
      resize(clampRatio(offset / Math.max(1, (horizontal ? split.width : split.height) - GAP)));
    };
    const end = () => {
      for (const [type, fn] of events) element.removeEventListener(type, fn);
      grid.classList.remove("ws-resizing");
      save();
    };
    const events = [["pointermove", move], ["pointerup", end], ["pointercancel", end], ["lostpointercapture", end]];
    for (const [type, fn] of events) element.addEventListener(type, fn);
  });
  return element;
}

function placeDivider(ws, d) {
  let entry = dividers.get(d.node);
  if (!entry) {
    entry = { element: makeDivider(d.node) };
    dividers.set(d.node, entry);
    grid.appendChild(entry.element);
  }
  entry.box = d.box;
  const { element } = entry;
  const horizontal = d.node.dir !== "v";
  const [before, after] = d.node.children.map((child) => leaves(child));
  const title = (id) => ws.panes[id]?.title || "agent";
  element.className = `ws-divider ${horizontal ? "horizontal" : "vertical"}`;
  element.setAttribute("aria-orientation", horizontal ? "vertical" : "horizontal");
  element.setAttribute("aria-valuenow", String(Math.round(clampRatio(d.node.ratio) * 100)));
  element.setAttribute("aria-controls", before.map((id) => `ws-pane-${id}`).join(" "));
  element.setAttribute("aria-label", `Resize ${title(before.at(-1))} and ${title(after[0])}`);
  box(element, d);
}

function layout() {
  const ws = current();
  const placed = new Set();
  for (const [id, f] of frames) {
    const visible = !home && f.ws === ws && (!zoomed || id === zoomed);
    f.element.hidden = !visible;
    f.element.classList.toggle("focused", id === ws?.focused);
    const button = f.element.querySelector('[data-action="zoom"]');
    button.textContent = zoomed === id ? "⤡" : "⛶";
    button.title = zoomed === id ? "Restore panes (Alt+Z)" : "Maximize pane (Alt+Z)";
    button.setAttribute("aria-label", zoomed === id ? "Restore panes" : "Maximize pane");
  }
  if (ws && !home) {
    const rect = { left: 0, top: 0, width: grid.clientWidth, height: grid.clientHeight };
    if (zoomed && frames.has(zoomed)) box(frames.get(zoomed).element, rect);
    else {
      const positions = layoutRects(ws.tree, rect, GAP, { min: MIN_RATIO, max: MAX_RATIO });
      for (const [id, r] of positions.leaves) { if (frames.has(id)) box(frames.get(id).element, r); }
      for (const d of positions.dividers) { placeDivider(ws, d); placed.add(d.node); }
    }
  }
  for (const [node, { element }] of dividers) {
    if (!placed.has(node)) { element.remove(); dividers.delete(node); }
  }
}

function render() {
  const ws = current();
  attention.retain(new Set(frames.keys()));
  document.title = badgeTitle(!home && ws ? `${ws.name || ws.project.name} | QuickCode` : "QuickCode", attention.total());
  document.getElementById("ws-title").textContent = home ? "Projects" : ws?.name || ws?.project.name || "Workspace";
  document.getElementById("ws-path").textContent = home ? "Open a folder or return to a workspace" : ws?.project.path || "";
  document.getElementById("ws-new-agent").disabled = home || !ws?.ready;
  document.getElementById("ws-undo").hidden = !undo;
  document.getElementById("ws-empty").hidden = home || !ws || !!ws.tree;
  // Sidebar nodes are keyed. Status updates and resize must not destroy focus.
  for (const workspace of workspaces.values()) {
    const pid = workspace.project.id;
    let group = [...sidebar.children].find((n) => n.dataset.project === pid);
    if (!group) {
      group = el(`<section class="ws-group"><div class="ws-project-row"><button class="ws-project"><span aria-hidden="true">▱</span><span class="ws-project-name"></span><span class="ws-count"></span></button><button class="ws-icon ws-manage" aria-label="Manage workspace" title="Manage workspace">⋯</button></div><div class="ws-agents"></div></section>`);
      group.dataset.project = pid; sidebar.appendChild(group);
      group.querySelector(".ws-project").onclick = () => { if (active !== pid || home) activate(workspace); };
      group.querySelector(".ws-manage").onclick = () => manageWorkspace(workspace);
    }
    group.querySelector(".ws-project-name").textContent = workspace.name || workspace.project.name || workspace.project.path;
    group.querySelector(".ws-project").title = workspace.project.path;
    group.querySelector(".ws-project").setAttribute("aria-current", !home && pid === active ? "true" : "false");
    group.querySelector(".ws-count").textContent = leaves(workspace.tree).length;
    const list = group.querySelector(".ws-agents");
    const ids = leaves(workspace.tree);
    paintBadge(group.querySelector(".ws-project"), attention.sum(ids), group.querySelector(".ws-count"));
    for (const node of [...list.children]) { if (!ids.includes(node.dataset.id)) node.remove(); }
    ids.forEach((id) => {
      const pane = workspace.panes[id];
      let row = [...list.children].find((n) => n.dataset.id === id);
      if (!row) {
        row = el(`<button class="ws-agent"><span class="ws-dot" aria-hidden="true"></span><span class="ws-agent-name"></span><span class="ws-agent-state"></span></button>`);
        row.dataset.id = id; list.appendChild(row);
        row.onclick = () => focus(workspace, id);
      }
      row.querySelector(".ws-agent-name").textContent = pane.title;
      row.title = pane.title;
      const selected = !home && active === pid && workspace.focused === id;
      row.classList.toggle("active", selected);
      row.setAttribute("aria-current", String(selected));
      paintBadge(row, attention.get(id), row.querySelector(".ws-agent-state"));
    });
  }
  for (const [id, f] of frames) {
    paintBadge(f.element.querySelector(".ws-pane-head"), attention.get(id), f.element.querySelector(".ws-pane-status"));
  }
  layout();
}

function dialog(title, body) {
  const modal = el(`<dialog class="ws-dialog"><form method="dialog"><header><h2>${esc(title)}</h2><button class="ws-icon" value="cancel" aria-label="Close dialog">×</button></header></form>${body}</dialog>`);
  document.body.appendChild(modal);
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.close(); });
  modal.addEventListener("close", () => modal.remove());
  modal.showModal();
  return modal;
}

function renamePane(ws, pane) {
  const modal = dialog("Rename conversation", `<form class="ws-form"><label>Name<input name="title" maxlength="120" value="${esc(pane.title)}" required></label><p class="ws-error" role="alert"></p><button class="btn primary" type="submit">Save name</button></form>`);
  const form = modal.querySelector(".ws-form");
  const input = form.elements.title;
  input.focus(); input.select();
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (!pane.convId) { modal.querySelector(".ws-error").textContent = "Wait for the agent to connect, then try again."; return; }
    form.querySelector("button").disabled = true;
    try {
      const result = await api.renameSessionOf(ws.project.id, pane.convId, input.value.trim());
      pane.title = result.title || input.value.trim();
      post(frames.get(pane.id)?.iframe, { action: "renamed" });
      updateTitle(pane); render(); save(); modal.close();
    } catch (err) { modal.querySelector(".ws-error").textContent = err.message; }
    finally { form.querySelector("button").disabled = false; }
  };
}

function updateTitle(pane) {
  const f = frames.get(pane.id);
  if (!f) return;
  f.element.querySelector(".ws-pane-name").textContent = pane.title;
  f.element.setAttribute("aria-label", `Agent: ${pane.title}`);
  f.iframe.title = pane.title;
}

function appearance() {
  const modal = dialog("Workspace appearance", `<div class="ws-appearance-controls"></div>`);
  renderAppearanceControls(modal.querySelector(".ws-appearance-controls"));
}

function manageWorkspace(ws) {
  const modal = dialog("Manage workspace", `<form class="ws-form"><label>Workspace name<input name="name" maxlength="80" value="${esc(ws.name || ws.project.name)}" required></label>
    <p>${esc(ws.project.path)}</p><button class="btn primary" type="submit">Save name</button>
    <button class="btn" type="button" data-equal>Equalize pane sizes</button>
    <p>Closing this workspace keeps its conversations in project history. Running agents continue. Personal terminal shells close.</p>
    <button class="btn" type="button" data-close>Close workspace</button></form>`);
  const form = modal.querySelector(".ws-form");
  form.onsubmit = (e) => { e.preventDefault(); ws.name = form.elements.name.value.trim(); render(); save(); modal.close(); };
  modal.querySelector("[data-equal]").onclick = () => {
    equalize(ws.tree); zoomed = null; render(); save(); modal.close();
  };
  modal.querySelector("[data-close]").onclick = () => {
    for (const [id, f] of frames) if (f.ws === ws) { f.element.remove(); frames.delete(id); }
    workspaces.delete(ws.project.id);
    if (undo?.ws === ws) undo = null;
    [...sidebar.children].find((n) => n.dataset.project === ws.project.id)?.remove();
    modal.close();
    if (active === ws.project.id) { active = null; showHome(); }
    else render();
    save();
  };
}

function openUtility(route, pid = home ? null : active) {
  if (utility) utility.remove();
  utility = el(`<dialog class="ws-utility" aria-label="Settings and help"><iframe title="Settings and help"></iframe></dialog>`);
  const query = new URLSearchParams({ pane: "1", utility: "1" });
  if (pid) query.set("project", pid);
  utility.querySelector("iframe").src = `${location.pathname}?${query}${route}`;
  shell.appendChild(utility);
  utility.addEventListener("close", () => { utility?.remove(); utility = null; acknowledge(); });
  utility.showModal();
}

function shortcut(e) {
  if (!e.altKey || e.ctrlKey || e.metaKey || document.querySelector("dialog[open]") || utility) return;
  const key = e.key.toLowerCase();
  if (key === "n" && !home) { e.preventDefault(); addPane(); }
  else if (key === "z" && !home) { e.preventDefault(); toggleZoom(); }
  else if (key === "b") { e.preventDefault(); toggleSidebar(); }
  else if (["arrowleft", "arrowright", "arrowup", "arrowdown"].includes(key) && !home) {
    e.preventDefault();
    const ws = current(), ids = leaves(ws.tree), index = ids.indexOf(ws.focused);
    const next = ["arrowleft", "arrowup"].includes(key) ? -1 : 1;
    if (ids.length) focus(ws, ids[(index + next + ids.length) % ids.length]);
  }
}

export async function bootWorkspaces() {
  const route = location.hash;
  const launch = initAuth();
  document.body.classList.add("workspace-shell");
  shell = el(`<div id="workspace-shell" class="ws-home">
    <aside id="ws-sidebar" class="ws-sidebar"><div class="ws-brand"><img src="assets/icon.svg" width="25" height="25" alt=""><strong>QuickCode</strong><span>Workspace</span></div>
      <button class="ws-open btn" id="ws-open">＋ Open folder</button>
      <div class="ws-section-label">Workspaces</div><nav id="ws-list" aria-label="Workspaces and agents"></nav>
      <div class="ws-sidebar-bottom"><button id="ws-projects">All projects</button><button id="ws-appearance">Appearance</button><button id="ws-settings">Settings</button><button id="ws-help">Help & shortcuts</button></div>
      <div class="ws-sidebar-grip" role="separator" tabindex="0" aria-label="Resize workspace sidebar" aria-orientation="vertical" aria-valuemin="160" aria-valuemax="360" aria-valuenow="232"></div>
    </aside>
    <div class="ws-content"><header class="ws-toolbar"><button id="ws-sidebar-toggle" class="ws-icon" title="Toggle sidebar (Alt+B)" aria-label="Toggle sidebar" aria-controls="ws-sidebar">☰</button><div class="ws-location"><strong id="ws-title">Projects</strong><span id="ws-path"></span></div><button id="ws-undo" class="btn" hidden>Reopen closed pane</button><button id="ws-new-agent" class="btn primary" title="New agent pane (Alt+N)">＋ New agent</button></header>
      <main id="ws-grid" aria-label="Agent workspace"><div id="ws-empty" hidden><h2>Your workspace is ready</h2><p>Open an agent to start a conversation in this folder.</p><button class="btn primary">New agent</button></div></main>
      <div id="ws-live" class="sr-only" aria-live="polite"></div>
    </div></div>`);
  document.body.appendChild(shell);
  grid = document.getElementById("ws-grid"); sidebar = document.getElementById("ws-list");
  initSidebarResize();
  // Home remains the existing project browser, including history and recovery.
  shell.querySelector(".ws-content").appendChild(document.getElementById("app"));
  initHome({ onOpen: openProject });
  document.getElementById("ws-open").onclick = () => openDirBrowser(openProject);
  document.getElementById("ws-projects").onclick = showHome;
  document.getElementById("ws-new-agent").onclick = () => addPane();
  document.querySelector("#ws-empty button").onclick = () => addPane();
  document.getElementById("ws-undo").onclick = undoClose;
  document.getElementById("ws-appearance").onclick = appearance;
  document.getElementById("ws-settings").onclick = () => openUtility("#/config/install/general");
  document.getElementById("ws-help").onclick = () => openUtility("#/help/workspaces");
  document.getElementById("ws-sidebar-toggle").onclick = toggleSidebar;
  document.getElementById("home-settings").onclick = () => openUtility("#/config/install/general");
  document.getElementById("home-help").onclick = () => openUtility("#/help/workspaces");
  new ResizeObserver(layout).observe(grid);
  document.addEventListener("keydown", shortcut);
  window.addEventListener("focus", acknowledge);
  document.addEventListener("visibilitychange", acknowledge);
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || e.data?.source !== "qc-agent") return;
    const f = [...frames.values()].find((f) => f.iframe.contentWindow === e.source);
    const isUtility = utility?.querySelector("iframe").contentWindow === e.source;
    if (!f && !isUtility) return;
    const data = e.data;
    if (data.action === "utility-close" && isUtility) { utility.close(); return; }
    if (!f) return;
    if (data.action === "focus") {
      if (current()?.focused !== f.pane.id || active !== f.ws.project.id) focus(f.ws, f.pane.id, false);
      else acknowledge();
    }
    else if (data.action === "notice") notice(f, data);
    else if (data.action === "home") showHome();
    else if (data.action === "settings" && /^#\/(config|help)/.test(data.route)) openUtility(data.route, f.ws.project.id);
    else if (data.action === "shortcut") shortcut({ ...data, preventDefault() {} });
    else if (data.action === "open-session") { focus(f.ws, f.pane.id, false); addPane(f.ws, null, data.convId, data.seq); }
    else if (data.action === "state") {
      if (data.convId) f.pane.convId = data.convId;
      if (data.persisted) f.pane.persisted = true;
      if (data.title) f.pane.title = data.title;
      updateTitle(f.pane);
      const state = data.connection !== "open" ? "Offline" : data.review ? "Needs approval" : data.busy ? "Working" : "Ready";
      f.element.dataset.state = state;
      f.element.querySelector(".ws-pane-status").textContent = state;
      render();
      const row = [...sidebar.querySelectorAll(".ws-agent")].find((n) => n.dataset.id === f.pane.id);
      if (row) { row.dataset.state = state; row.querySelector(".ws-agent-state").textContent = state; }
      save();
    }
    else if (data.action === "theme") applyTheme(data.theme);
  });
  try {
    const saved = JSON.parse(localStorage.getItem(KEY));
    if (saved?.version === 1 && Array.isArray(saved.workspaces)) {
      for (const item of saved.workspaces.slice(0, 30)) {
        const ws = restoreWorkspace(item);
        if (ws) workspaces.set(ws.project.id, ws);
      }
      active = saved.active;
    }
  } catch { /* malformed storage starts with the project browser */ }
  render();
  if (!authToken()) { showHome(); return; }
  try {
    const bs = await api.bootstrap(); applyTheme(bs.theme);
    const data = await api.projects();
    // A project explicitly forgotten from Home must not return after reload,
    // and the registry, not the saved layout, says which folder an id opens.
    const known = new Map(data.projects.map((p) => [p.id, p]));
    for (const [id, ws] of workspaces) {
      const entry = known.get(id);
      if (entry) ws.project = { id, path: entry.path, name: entry.name };
      else workspaces.delete(id);
    }
    sidebar.replaceChildren();
    if (launch.project) {
      const p = data.projects.find((p) => p.id === launch.project);
      if (p) { await api.openProject(p.path); await openProject(p, { resume: launch.resumeHint }); }
      else showHome();
    } else if (workspaces.has(active)) await activate(current());
    else showHome();
  } catch (err) { showHome(); toastError(err.message); }
  if (/^#\/(config|help)/.test(route)) openUtility(route);
}
