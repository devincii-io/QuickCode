// Usage panel: where the tokens went. Aggregates the logged "usage" events by
// turn so a single expensive turn is visible next to the running ledger the
// status bar shows.

import { store, subscribe } from "../store.js";
import { esc, fmtCost, fmtTokens } from "../util.js";

export const panel = {
  id: "usage",
  title: "Usage",
  icon: "Σ",
  init(container) {
    container.classList.add("panel-usage");
    container.innerHTML = `<div class="pu-live"></div><div class="pu-table"></div>`;
    const liveEl = container.querySelector(".pu-live");
    const tableEl = container.querySelector(".pu-table");

    function render() {
      liveEl.innerHTML = liveHtml(store.state?.ledger);
      tableEl.innerHTML = tableHtml(byTurn(store.events));
    }

    render();
    subscribe((kind, ev) => {
      if (kind === "reset" || kind === "state") { render(); return; }
      if (kind === "event" && ev.type === "usage") render();
    });
  },
};

// ---- aggregation ----

function byTurn(events) {
  const rows = new Map();
  for (const ev of events) {
    if (ev.type !== "usage") continue;
    const turn = ev.turn ?? 0;
    const row = rows.get(turn) || { turn, input: 0, output: 0, cached: 0, cost: 0 };
    row.input += ev.input_tokens || 0;
    row.output += ev.output_tokens || 0;
    row.cached += ev.cached_tokens || 0;
    row.cost += ev.cost_usd || 0;
    rows.set(turn, row);
  }
  return [...rows.values()].sort((a, b) => b.turn - a.turn);
}

function totals(rows) {
  return rows.reduce((t, r) => ({
    input: t.input + r.input, output: t.output + r.output,
    cached: t.cached + r.cached, cost: t.cost + r.cost,
  }), { input: 0, output: 0, cached: 0, cost: 0 });
}

// ---- rendering ----

function liveHtml(ledger) {
  if (!ledger) return `<div class="pu-empty">Not connected.</div>`;
  const cells = [
    ["in", fmtTokens(ledger.input_tokens)],
    ["out", fmtTokens(ledger.output_tokens)],
    ["cached", fmtTokens(ledger.cached_tokens)],
    ["cost", fmtCost(ledger.cost_usd)],
  ];
  return cells.map(([k, v]) =>
    `<div class="pu-stat"><span class="pu-k">${esc(k)}</span>
     <span class="pu-v">${esc(v)}</span></div>`).join("");
}

function tableHtml(rows) {
  if (!rows.length) return `<div class="pu-empty">No usage recorded yet.</div>`;
  const t = totals(rows);
  const body = rows.map((r) => `<tr>
      <td class="pu-turn">${esc(r.turn)}</td>
      <td>${esc(fmtTokens(r.input))}</td>
      <td>${esc(fmtTokens(r.output))}</td>
      <td>${esc(fmtTokens(r.cached))}</td>
      <td>${esc(fmtCost(r.cost))}</td>
    </tr>`).join("");
  return `<table class="pu-grid">
    <thead><tr><th>turn</th><th>in</th><th>out</th><th>cached</th><th>cost</th></tr></thead>
    <tbody>${body}</tbody>
    <tfoot><tr>
      <td class="pu-turn">all</td>
      <td>${esc(fmtTokens(t.input))}</td>
      <td>${esc(fmtTokens(t.output))}</td>
      <td>${esc(fmtTokens(t.cached))}</td>
      <td>${esc(fmtCost(t.cost))}</td>
    </tr></tfoot></table>`;
}
