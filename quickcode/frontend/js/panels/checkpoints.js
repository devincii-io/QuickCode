// Checkpoints panel: every turn of this conversation that changed files, what
// each did to them, and a way into the rewind dialog for turns whose message
// is scrolled far away. Read-only apart from that button; it asks the server
// (GET …/checkpoints) rather than rebuilding the index from the log, because
// the index knows what was evicted and what a rewind already undid.

import { api } from "../api.js";
import { openRewindDialog } from "../checkpoints/dialog.js";
import { fmtBytes, listingRows } from "../checkpoints/model.js";
import { store, subscribe } from "../store.js";
import { h } from "../ui/dom.js";
import { debounce, fmtTime } from "../util.js";

const TOUCHES = new Set(["checkpoint", "files_rewound"]);

export const panel = {
  id: "checkpoints",
  title: "Checkpoints",
  icon: "↺",
  init(container) {
    container.classList.add("panel-checkpoints");
    const sum = h("span", { class: "pc-sum" }, "…");
    const list = h("div", { class: "pc-list" });
    container.replaceChildren(
      h("div", { class: "pc-head" }, sum,
        h("button", { class: "pc-refresh", type: "button", title: "Refresh",
          "aria-label": "Refresh the checkpoint list", onclick: () => refresh() }, "⟳")),
      list);

    let asked = 0;   // only the newest answer is drawn
    async function refresh() {
      const convId = store.convId;
      const mine = ++asked;
      if (!convId) { show(null); return; }
      let data;
      try {
        data = await api.checkpoints(convId);
      } catch (err) {
        if (mine !== asked) return;
        // A conversation nothing has been said in has no log yet: that is
        // "no checkpoints", not an error worth a red line.
        if (err?.status === 404) { show({ checkpoints: [] }); return; }
        sum.textContent = "unavailable";
        list.replaceChildren(h("div", { class: "pc-empty" }, String(err.message || err)));
        return;
      }
      if (mine === asked) show(data);
    }

    function show(data) {
      const rows = listingRows(data);
      const storage = data?.storage;
      sum.textContent = rows.length
        ? `${rows.length} ${rows.length === 1 ? "turn" : "turns"} · ${fmtBytes(storage?.used_bytes)} kept`
        : "nothing checkpointed";
      if (!rows.length) {
        list.replaceChildren(h("div", { class: "pc-empty" },
          "No file changes checkpointed in this conversation yet. Changes made by write, "
          + "edit and tools that declare the file they write are; bash changes are not."));
        return;
      }
      list.replaceChildren(...rows.map(turnNode),
        h("div", { class: "pc-untracked" }, data.untracked || ""));
    }

    function turnNode(row) {
      const button = h("button", {
        class: "btn pc-rewind", type: "button", disabled: !row.rewindable,
        "aria-label": `Rewind files to before turn ${row.turn}`,
        title: row.rewindable ? "Preview putting these files back, then choose"
          : "Nothing left to put back from this turn",
        onclick: () => openRewindDialog(row.turn, { opener: button }),
      }, "Rewind…");
      return h("section", { class: "pc-turn" },
        h("div", { class: "pc-turn-head" },
          h("strong", {}, `Turn ${row.turn}`),
          h("span", { class: "pc-time" }, fmtTime(row.time)),
          button),
        h("ul", { class: "pc-files" }, row.files.map((f) =>
          h("li", { class: f.state ? "pc-file pc-gone" : "pc-file" },
            h("span", { class: "pc-change", "data-change": f.change }, f.change),
            h("span", { class: "pc-path", title: f.path }, f.path),
            f.counts ? h("span", { class: "pc-counts" }, f.counts) : null,
            f.agent ? h("span", { class: "pc-agent" }, f.agent) : null,
            f.state ? h("span", { class: "pc-state" }, f.state) : null))));
    }

    const bump = debounce(refresh, 300);
    subscribe((kind, ev) => {
      if (kind === "reset") { asked++; show(null); return; }
      if (kind === "replay_done") { refresh(); return; }
      if (kind !== "event" || store.replaying) return;
      const type = ev.type === "agent_event" ? ev.ev?.type : ev.type;
      if (TOUCHES.has(type)) bump();
    });
    refresh();
  },
};
