// Help ▸ Your first session.
//
// A walkthrough in this app's own vocabulary — every control named here is a
// control that exists, spelled the way it is spelled on screen. The order is
// the order that actually works: you cannot get an answer before there is an
// endpoint, and you should not get a write before you have seen what a
// permission prompt looks like.
//
// The one editorial choice: it starts in plan mode rather than in ask mode,
// even though ask is the default. Watching the agent produce a plan you approve
// is the fastest way to understand that the gate is real, and it costs one
// keystroke to get there.

import { esc } from "../util.js";
import { link, note, pageHtml, sub } from "./ui.js";

const STEPS = [
  {
    title: "Open a folder as a project",
    body: `Everything is scoped to one project: its sessions, its permission
      rules, its trust decision. On the <strong>Home</strong> screen, pick a
      recent project or press <strong>＋ New project</strong> and browse to a
      folder. Folders that are git repositories are marked, because that is
      usually the one you want.`,
    doing: `Home → ＋ New project → choose a folder → <strong>Open this
      folder</strong>.`,
  },
  {
    title: "Answer the trust banner, if one appears",
    body: `If that project declares MCP servers of its own, or command tools in
      <code>.quickcode/plugins/</code>, a strip appears under the top bar naming
      each one and the exact command it would run. Until you approve, those
      things are listed and inert. Nothing else about the project is blocked, so
      you can read this at your own pace.`,
    doing: `Read the commands, then approve — or leave it. You can change your
      mind later; the chip in the top bar keeps the question in reach.`,
  },
  {
    title: "Point it at a provider",
    body: `The agent needs somewhere to send requests. The
      <strong>⚙ quick</strong> pill next to the composer opens the three
      install-level things worth changing without leaving the chat: the provider
      endpoint, the API key, and the theme. The key is stored encrypted at rest
      and is never read back out to the browser.`,
    doing: `⚙ quick → base URL and API key → <strong>Save</strong>. It applies to
      new sessions.`,
  },
  {
    title: "Choose a model",
    body: `The pill in the composer showing a model name opens the catalog, with
      a search box because it is long. The catalog is a convenience rather than a
      gate — the footer lets you type any id your provider accepts, listed or
      not.`,
    doing: `Click the model pill, or type <code>/model</code>.`,
  },
  {
    title: "Start in plan mode and ask for something real",
    body: `Plan mode is read-only: the mutating tools are not offered to the model
      at all, so it explores and then submits a plan for you to read. This is the
      cheapest way to find out whether it has understood your project before it
      starts changing it.`,
    doing: `Type <code>/mode plan</code>, then ask something concrete — “find
      where sessions are written to disk and tell me what you would change to add
      a retention limit”.`,
  },
  {
    title: "Read the plan, then approve it into a working mode",
    body: `The plan arrives as a review dialog with three answers. <em>Keep
      planning</em> sends feedback and it tries again. <em>Approve · ask mode</em>
      lets it work but stops at every mutating action. <em>Approve · auto-edit</em>
      lets file edits inside the project go through on their own — shell commands
      still ask.`,
    doing: `Approve into <strong>ask mode</strong> the first time. Watching the
      prompts is how you learn what it wants to do.`,
  },
  {
    title: "Answer a permission prompt — and understand “Always allow”",
    body: `The dialog shows the tool and the exact thing it would act on.
      <em>Allow once</em> is a one-off. <em>Always allow</em> writes a rule to
      <code>.quickcode/settings.local.json</code> — the dialog shows you the rule
      before you agree to it, and that file is gitignored and editable, so this
      is never a decision you cannot take back. <em>Deny</em> can carry a
      sentence, and that sentence goes to the agent, so it is a way to steer
      rather than just to refuse.`,
    doing: `Read the rule text on the <em>Always allow</em> line before pressing
      it. It is broader than the single call you are looking at.`,
  },
  {
    title: "Watch it work in the trajectory",
    body: `The <strong>⧉ Panel</strong> button opens the side panel. Its
      <em>Trajectory</em> tab is a real time axis over everything that happened,
      backed by the session's append-only log rather than by anything the browser
      remembered. Click any event for its payload, result and timing; press
      <strong>⛶</strong> to give the panel the whole window and <kbd>Esc</kbd> to
      bring the chat back.`,
    doing: `Panel → Trajectory. The ⌕ links in the transcript jump straight to the
      matching event.`,
  },
  {
    title: "Compact when the conversation gets long",
    body: `The status bar shows a context percentage. When it climbs, the older
      part of the conversation is replaced by a structured summary and the most
      recent turns are carried through word for word. It happens between turns,
      never in the middle of one — and you can ask for it yourself at any
      time.`,
    doing: `<code>/compact</code>, or the <strong>⇊ compact</strong> pill.`,
  },
  {
    title: "Then go and look at Settings",
    body: `Now that you have seen a turn, the configuration view will read as a
      map instead of a wall. Start at <em>Agents</em> — that is the agent you have
      been talking to, with the exact prompt it received and the exact tool
      schemas it was handed, not a description of them.`,
    doing: `The ⚙ button in the top bar, or ${link("#/config/agents",
      "open it now")}.`,
  },
];

export async function renderTutorial(host) {
  const body = `
    ${sub("Ten steps, in order")}
    <ol class="hp-steps">
      ${STEPS.map((s) => `<li class="hp-step">
        <h4>${esc(s.title)}</h4>
        <p class="hp-p">${s.body}</p>
        <div class="hp-step-do">${s.doing}</div>
      </li>`).join("")}
    </ol>

    ${sub("Three habits worth forming early")}
    <ul class="hp-list">
      <li class="hp-li"><strong>Plan first on anything you care about.</strong>
        A plan is cheap to reject and a wrong edit is not.</li>
      <li class="hp-li"><strong>Read the rule, not the button.</strong> Every
        <em>Always allow</em> is broader than the call in front of you — that is
        the point of it — so the rule text is the thing to check.</li>
      <li class="hp-li"><strong>Give a denial a reason.</strong> The sentence you
        type into a denial reaches the agent. “No — use the existing helper in
        utils” gets you a better next attempt than a bare refusal.</li>
    </ul>

    ${note("Sessions, and what a new one costs", `
      <p class="hp-p">The chip in the top bar switches sessions and starts new
        ones. A session's composition — which tools, which prompt, which
        ceiling — is fixed when it opens, so a configuration change you make now
        reaches the <em>next</em> session rather than this one. That is why
        several pages in Settings offer to start a fresh conversation right after
        you save something. Old sessions are files: they can be archived, which
        moves them out of the list, or deleted, which does not come back.</p>`)}

    <p class="hp-p">Next: ${link("#/help/permissions", "Permissions & trust")}
      for what the gate actually checks, or ${link("#/help/handson", "Hands-on")}
      to try a rule without spending a token.</p>
  `;

  host.innerHTML = pageHtml("Your first session", {
    crumb: "Help",
    sigil: "1.",
    lede: `A first run through, in the order that works. Every button named here
      exists and is spelled the way it is spelled on screen.`,
    body,
  });
}
