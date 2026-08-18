// Empty states.
//
// One rule, applied everywhere: an empty state names **one real thing that
// already exists**, says in one sentence what you would change about it, and
// offers the button that starts from it. "No tools yet" is a fact about the
// screen; "bash runs whatever the model wrote — a command tool is that with
// the command already decided" is a fact about the product, and it is the one
// that gets somebody to press something.
//
// Every id named here is a plugin that ships with QuickCode, so the copy
// cannot go stale silently: if `agent.explore` is ever renamed, the button
// stops resolving and the empty state is where it shows up first.

import { esc } from "../util.js";

/** A button that duplicates a named plugin, wired by `wireEmpty`. */
function dupBtn(id, label) {
  return `<button class="btn primary" data-empty-dup="${esc(id)}">⧉ ${esc(label)}</button>`;
}

function newBtn(kind, label) {
  return `<a class="btn" href="#/config/new/${esc(kind)}">+ ${esc(label)}</a>`;
}

const STATES = {
  tools: () => `<p>No tools of your own yet — everything on this page is
      built into QuickCode.</p>
    <p><code>tool.bash</code> is the general one: it runs whatever the model
      wrote, which is why every call stops for permission. A <b>command tool</b>
      is that with the command already decided — <code>uv run pytest -q
      {path}</code> rather than an open shell — so the approval prompt shows the
      exact argv and nothing the model fills in can add a token.</p>
    <div class="empty-actions">${newBtn("tool", "New command tool")}</div>`,

  prompt: () => `<p>No prompt sections of your own yet.</p>
    <p><code>prompt.conventions</code> is the section that tells the model how
      to write code here. Duplicate it and you get a sibling that renders
      <i>after</i> it — the original still applies, you are adding a voice
      rather than replacing one — with its text as plain editable markdown.</p>
    <div class="empty-actions">
      ${dupBtn("prompt.conventions", "Duplicate Conventions")}
      ${newBtn("prompt", "New prompt section")}
    </div>`,

  agents: () => `<p>No agents of your own yet.</p>
    <p><code>agent.explore</code> is the built-in researcher: read, glob and
      grep, no write, and a ceiling it can never be raised above. Duplicate it
      and every line of it — including the ones that are locked here — becomes
      plain text in a file you own.</p>
    <div class="empty-actions">
      ${dupBtn("agent.explore", "Duplicate Explore")}
      ${newBtn("agent", "New agent")}
    </div>`,

  mcp: () => `<p>No MCP servers configured.</p>
    <p>An MCP server is an external process that contributes tools. Paste a
      Claude-style block under <code>mcpServers</code> in
      <code>.quickcode/settings.json</code> and it shows up here as a plugin,
      with its command readable and its tools listed on the Tools page. A
      project's own servers stay inert until you trust the project once,
      because starting one runs its command on this machine.</p>`,

  models: () => `<p>No providers are installed beyond the built-in one.</p>
    <p>Providers are Python packages discovered through the
      <code>quickcode.providers</code> entry point — there is nothing to author
      here. The endpoint and the key live under
      <a class="k-link" href="#/config/install">Install</a>.</p>`,

  policies: () => `<p>Nothing of this kind is registered.</p>`,
};

/** The empty state for a Parts page. */
export function emptyHtml(slug) {
  const body = (STATES[slug] || STATES.policies)();
  return `<div class="set-empty empty-state">${body}</div>`;
}

/** The empty state for a filter that matched nothing — a different sentence,
 *  because "you have none" and "none match" are different situations and
 *  showing the first for the second is a lie about the project. */
export function emptyFilterHtml(slug, { source = "", scope = "" } = {}) {
  // Scope is checked first because it is the more specific claim: "you have
  // none of these" and "you have none of these *here*" are different, and the
  // second is the one that answers a scope filter.
  if (scope) {
    return `<div class="set-empty empty-state">
      <p>Nothing of yours is in <b>${esc(scope)}</b> scope.</p>
      <p>${scope === "user"
        ? `User scope is <code>~/.quickcode/plugins/</code> — files that follow
           you into every project. Project scope is
           <code>.quickcode/plugins/</code>, which is committed and travels
           with the repository instead.`
        : `Project scope is <code>.quickcode/plugins/</code>, committed and
           shared with anyone who clones this repository. User scope is
           <code>~/.quickcode/plugins/</code>, which is yours alone.`}</p>
    </div>`;
  }
  if (source === "authored") {
    const body = (STATES[slug] || STATES.policies)();
    return `<div class="set-empty empty-state">
      <p class="empty-lead">Nothing here is yours yet.</p>${body}</div>`;
  }
  return `<div class="set-empty">Nothing matches that.</div>`;
}

/** Wire the Duplicate buttons an empty state offers. `duplicate` is the shared
 *  action from `create/scaffold.js`, passed in rather than imported, so this
 *  file stays free of the write path and can be rendered anywhere. */
export function wireEmpty(host, duplicate) {
  host.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-empty-dup]");
    if (!btn) return;
    e.preventDefault();
    duplicate(btn.dataset.emptyDup, btn);
  });
}
