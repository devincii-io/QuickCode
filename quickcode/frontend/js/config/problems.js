// Problems: the card pinned above every plugin list, and the page behind it.
//
// This exists to prevent one specific failure. A plugin whose file has an
// error-severity problem is **skipped**: it is not in the registry, not in the
// tool pool, not in the prompt — and therefore not in the list on this page
// either. Without this card the only evidence that you wrote something would
// be its absence, which is the hardest kind of bug to see. So the rule is:
//
//   a skipped plugin appears here and NOT in the plugin list.
//
// (`docs/design/AUTHORING.md` §5.3. The half-loaded alternative — leaving it
// in the list with a warning triangle — invites "is it running?", and the
// answer has to be unambiguous.)
//
// Every row carries the same four things the `Problem` shape carries: a
// severity, what is wrong, what to do about it, and where the file is. The
// path is the affordance that matters: an error you cannot navigate to is a
// notification, not a fix.

import { esc } from "../util.js";

const SEV_ORDER = { error: 0, warning: 1, info: 2 };

const SEV_NOTE = {
  error: "skipped — it is not loaded, so it is not in the list below either",
  warning: "loaded, but not what the file asked for",
  info: "nothing was lost; worth saying once",
};

/** The problems that belong to one Parts page, by the subject's id prefix. */
const PREFIX_PART = {
  "tool.": "tools",
  "prompt.": "prompt",
  "agent.": "agents",
  "mcp.": "mcp",
  "provider.": "models",
  "policy.": "policies",
  "runtime.": "policies",
  "hook.": "policies",
  "storage.": "policies",
  "panel.": "policies",
};

export function partOfProblem(problem) {
  const subject = String(problem?.subject || "");
  for (const [prefix, part] of Object.entries(PREFIX_PART)) {
    if (subject.startsWith(prefix)) return part;
  }
  return "";                       // project-wide, or a file that never got an id
}

export function sortProblems(problems) {
  return [...(problems || [])].sort(
    (a, b) => (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b.severity] ?? 3));
}

export function countBySeverity(problems) {
  const out = { error: 0, warning: 0, info: 0 };
  for (const p of problems || []) if (p.severity in out) out[p.severity] += 1;
  return out;
}

/** Errors and warnings only — the count the rail wears. An info row is worth
 *  a page, never a badge: a badge that is always lit stops being read. */
export function alertCount(problems) {
  const n = countBySeverity(problems);
  return n.error + n.warning;
}

function pathOf(problem) {
  return problem?.provenance?.path || "";
}

function rowHtml(problem) {
  const path = pathOf(problem);
  const where = [
    problem.subject ? `<code class="pb-subject">${esc(problem.subject)}</code>` : "",
    problem.field ? `<span class="pb-field">${esc(problem.field)}</span>` : "",
    problem.line ? `<span class="pb-line">line ${esc(problem.line)}</span>` : "",
  ].filter(Boolean).join("");
  return `<div class="pb-row" data-sev="${esc(problem.severity)}"
      data-path="${esc(path)}" data-subject="${esc(problem.subject || "")}">
    <span class="pb-sev" title="${esc(SEV_NOTE[problem.severity] || "")}"
      >${esc(problem.severity)}</span>
    <div class="pb-main">
      <div class="pb-msg">${esc(problem.message)}</div>
      ${problem.fix ? `<div class="pb-fix">${esc(problem.fix)}</div>` : ""}
      <div class="pb-where">${where}
        ${path ? `<code class="pb-path" title="${esc(path)}">${esc(path)}</code>` : ""}
        <code class="pb-code">${esc(problem.code)}</code>
      </div>
    </div>
    <div class="pb-actions">
      ${path ? `<button class="ghost-btn" data-open-file>Open file</button>` : ""}
    </div>
  </div>`;
}

/** The pinned card. Returns "" when there is nothing to say — an empty
 *  Problems card teaches people to stop looking at the Problems card. */
export function problemsCardHtml(problems, { title = "Problems", note = "" } = {}) {
  const rows = sortProblems(problems);
  if (!rows.length) return "";
  const n = countBySeverity(rows);
  const worst = n.error ? "error" : n.warning ? "warning" : "info";
  return `<section class="pb-card" data-worst="${worst}">
    <div class="pb-head">
      <h4>${esc(title)} <span class="pb-count">${rows.length}</span></h4>
      <span class="pb-tally">
        ${n.error ? `<span class="pb-tally-bit" data-sev="error">${n.error} skipped</span>` : ""}
        ${n.warning ? `<span class="pb-tally-bit" data-sev="warning">${n.warning} loaded with a warning</span>` : ""}
        ${n.info ? `<span class="pb-tally-bit" data-sev="info">${n.info} for information</span>` : ""}
      </span>
    </div>
    ${n.error ? `<p class="pb-lede">An <b>error</b> means the plugin was not
      loaded. It is not in the list below, not in the tool pool and not in the
      prompt — this card is the only place it appears at all.</p>` : ""}
    ${note ? `<p class="pb-lede">${note}</p>` : ""}
    <div class="pb-rows">${rows.map(rowHtml).join("")}</div>
  </section>`;
}

/** Wire the Open file buttons. There is no "launch the OS editor" route and
 *  inventing one would be a new capability behind a small button, so this does
 *  the two things the browser can honestly do: open the authored file in the
 *  source editor when the problem belongs to a plugin we can address, and
 *  otherwise put the path on the clipboard. */
export function wireProblems(host, ctx) {
  host.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-open-file]");
    if (!btn) return;
    const row = btn.closest(".pb-row");
    const path = row?.dataset.path || "";
    const subject = row?.dataset.subject || "";
    const authored = (ctx.authored || []).find(
      (p) => p.id === subject || p.path === path);
    if (authored) { ctx.go(`#/config/edit/${encodeURIComponent(authored.id)}`); return; }
    try {
      await navigator.clipboard.writeText(path);
      btn.textContent = "Path copied";
    } catch {
      btn.textContent = path;      // no clipboard permission: show it to select
    }
    setTimeout(() => { btn.textContent = "Open file"; }, 2400);
  });
}

// ---- the page -------------------------------------------------------------

export function renderProblems(host, ctx) {
  const problems = ctx.kernel.problems || [];
  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">Problems</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="policy">!</span>
        <h2>Problems</h2>
        <span class="cfg-count">${problems.length}</span>
        <span class="cfg-head-actions">
          <button class="btn" data-recheck>Re-check</button>
        </span>
      </div>
    </header>
    <div class="cfg-lede">Everything this project got wrong, authoring and
      resolution alike, in one list. A validation problem and a resolution
      conflict are the same thing at two different times: something written
      down does not do what it looks like it does.</div>
    <div class="pb-slot">${problems.length
      ? problemsCardHtml(problems, { title: "All problems" })
      : `<div class="set-empty">Nothing is wrong. Every plugin file in this
           project and in your user scope parses, and every composition
           resolves.</div>`}</div>
  </div>`;

  wireProblems(host, ctx);
  host.querySelector("[data-recheck]")?.addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true;
    btn.textContent = "Re-checking…";
    try {
      const fresh = await ctx.api.kernelProblems();
      ctx.kernel.problems = fresh.problems || [];
      renderProblems(host, ctx);
      ctx.railDirty?.();
    } catch {
      btn.disabled = false;
      btn.textContent = "Re-check";
    }
  });
}
