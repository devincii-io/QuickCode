// Usage panel: where the tokens went. Aggregates the logged usage records --
// the main agent's "usage" events and every subagent's, which ride one level
// down inside an "agent_event" wrapper -- into two views: by turn, so a single
// expensive turn is visible next to the running ledger the status bar shows,
// and by agent, so a fan-out of ten workers is legible as ten rows instead of
// one unexplained spike.

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
      const rows = collect(store.events);
      liveEl.innerHTML = liveHtml(store.state?.ledger);
      tableEl.innerHTML = turnHtml(byTurn(rows)) + agentHtml(byAgent(rows));
    }

    render();
    subscribe((kind, ev) => {
      if (kind === "reset" || kind === "state") { render(); return; }
      if (kind !== "event") return;
      if (ev.type === "usage") render();
      else if (ev.type === "agent_event" && ev.ev?.type === "usage") render();
    });
  },
};

// ---- aggregation ----

// One flat record per logged usage event, tagged with who spent it.
function collect(events) {
  const out = [];
  for (const ev of events) {
    if (ev.type === "usage") {
      out.push(record(ev, ev.turn ?? 0, "main"));
    } else if (ev.type === "agent_event" && ev.ev?.type === "usage") {
      // The wrapper carries the *parent's* turn number, which is the honest
      // attribution: a subagent's spend belongs to the turn that spawned it.
      out.push(record(ev.ev, ev.turn ?? 0, ev.agent_id || "subagent"));
    }
  }
  return out;
}

function record(u, turn, agent) {
  return {
    turn,
    agent,
    sub: agent !== "main",
    input: u.input_tokens || 0,
    output: u.output_tokens || 0,
    cached: u.cached_tokens || 0,
    cost: u.cost_usd || 0,
  };
}

function blank(extra) {
  return { input: 0, output: 0, cached: 0, cost: 0, ...extra };
}

function fold(row, r) {
  row.input += r.input;
  row.output += r.output;
  row.cached += r.cached;
  row.cost += r.cost;
}

function byTurn(rows) {
  const turns = new Map();
  for (const r of rows) {
    const row = turns.get(r.turn) || blank({ turn: r.turn, sub: blank({}) });
    fold(row, r);
    if (r.sub) fold(row.sub, r);
    turns.set(r.turn, row);
  }
  return [...turns.values()].sort((a, b) => b.turn - a.turn);
}

function byAgent(rows) {
  const agents = new Map();
  for (const r of rows) {
    const row = agents.get(r.agent) || blank({ agent: r.agent, sub: r.sub });
    fold(row, r);
    agents.set(r.agent, row);
  }
  // Main first, then subagents in the order they first spent anything.
  return [...agents.values()].sort((a, b) => (a.sub ? 1 : 0) - (b.sub ? 1 : 0));
}

// Takes by-turn rows, so every row already carries its own subagent subtotal.
function totals(rows) {
  const t = blank({ sub: blank({}) });
  for (const r of rows) {
    fold(t, r);
    fold(t.sub, r.sub);
  }
  return t;
}

// The subagent share of a turn, as a percentage. Measured on cost where the
// provider reports one and on tokens where it does not, so the column stays
// meaningful on an endpoint that prices nothing.
function share(row) {
  const whole = row.cost || row.input + row.output;
  const part = row.cost ? row.sub.cost : row.sub.input + row.sub.output;
  if (!whole || !part) return "";
  return Math.round((100 * part) / whole) + "%";
}

function shareTitle(row) {
  return `subagents: ${fmtTokens(row.sub.input)} in · ${fmtTokens(row.sub.output)} out · ${fmtCost(row.sub.cost)}`;
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
  // Session spend counts subagents; the context meter deliberately does not.
  // Naming their share here is what stops a fan-out from reading as a mystery.
  const subIn = ledger.subagent_input_tokens || 0;
  const subOut = ledger.subagent_output_tokens || 0;
  if (subIn || subOut) {
    cells.push([
      "subagents",
      `${fmtTokens(subIn + subOut)} · ${fmtCost(ledger.subagent_cost_usd)}`,
    ]);
  }
  return cells.map(([k, v]) =>
    `<div class="pu-stat"><span class="pu-k">${esc(k)}</span>
     <span class="pu-v">${esc(v)}</span></div>`).join("");
}

function turnHtml(rows) {
  if (!rows.length) return `<div class="pu-empty">No usage recorded yet.</div>`;
  const t = totals(rows);
  const body = rows.map((r) => `<tr>
      <td class="pu-turn">${esc(r.turn)}</td>
      <td>${esc(fmtTokens(r.input))}</td>
      <td>${esc(fmtTokens(r.output))}</td>
      <td>${esc(fmtTokens(r.cached))}</td>
      <td>${esc(fmtCost(r.cost))}</td>
      <td class="pu-share" title="${esc(shareTitle(r))}">${esc(share(r))}</td>
    </tr>`).join("");
  return `<div class="pu-head">by turn</div>
    <table class="pu-grid">
    <thead><tr><th>turn</th><th>in</th><th>out</th><th>cached</th><th>cost</th>
      <th title="the part of this turn that subagents spent">subs</th></tr></thead>
    <tbody>${body}</tbody>
    <tfoot><tr>
      <td class="pu-turn">all</td>
      <td>${esc(fmtTokens(t.input))}</td>
      <td>${esc(fmtTokens(t.output))}</td>
      <td>${esc(fmtTokens(t.cached))}</td>
      <td>${esc(fmtCost(t.cost))}</td>
      <td class="pu-share" title="${esc(shareTitle(t))}">${esc(share(t))}</td>
    </tr></tfoot></table>`;
}

function agentHtml(rows) {
  // One row is the main agent alone: nothing was delegated, so a per-agent
  // breakdown would only repeat the table above it.
  if (rows.length < 2) return "";
  const body = rows.map((r) => `<tr class="${r.sub ? "pu-is-sub" : ""}">
      <td class="pu-turn">${esc(r.agent)}</td>
      <td>${esc(fmtTokens(r.input))}</td>
      <td>${esc(fmtTokens(r.output))}</td>
      <td>${esc(fmtTokens(r.cached))}</td>
      <td>${esc(fmtCost(r.cost))}</td>
    </tr>`).join("");
  return `<div class="pu-head">by agent</div>
    <table class="pu-grid">
    <thead><tr><th>agent</th><th>in</th><th>out</th><th>cached</th><th>cost</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}
