// Help ▸ Permissions & trust — the safety story.
//
// This page has to be exact. Every sentence below was checked against
// `quickcode/core/permissions.py`, `quickcode/core/hooks.py` and
// `quickcode/security/trust.py`, and where the shipped documentation and the
// code disagree, the code is what is described here.
//
// The ordering of the page follows the ordering of the actual decision: the
// mode is the default answer, the rules are the named exceptions, the protected
// paths sit in front of both, and the trust gate is a different question
// entirely — not "may the agent do this" but "may this project's files start a
// process on your machine at all".

import { esc } from "../util.js";
import { store } from "../store.js";
import { MODES } from "./modes.js";
import { honesty, link, note, pageHtml, quote, sub } from "./ui.js";

function modesHtml() {
  return `<div class="hp-widget">
    ${MODES.map((m) => `
      <div class="hp-mode">
        <div class="hp-mode-head">
          <code class="hp-mode-id" data-mode="${esc(m.id)}">${esc(m.id)}</code>
          <span class="hp-mode-title">${esc(m.title)}</span>
        </div>
        <p class="hp-mode-what">${esc(m.what)}</p>
        <dl class="hp-mode-grid">
          <dt>Mutating tool</dt><dd>${esc(m.write)}</dd>
          <dt>Read-only tool</dt><dd>${esc(m.read)}</dd>
          <dt>Shell command</dt><dd>${esc(m.shell)}</dd>
          <dt>Protected path</dt><dd>${esc(m.protected)}</dd>
        </dl>
        ${m.caveat ? `<p class="hp-mode-caveat">${esc(m.caveat)}</p>` : ""}
      </div>`).join("")}
  </div>`;
}

export async function renderPermissions(host) {
  const body = `
    ${sub("The order the answer is decided in")}
    <p class="hp-p">One call, five checks, and the first one that answers wins.
      Knowing the order explains almost every surprising decision.</p>
    <ol class="hp-order">
      <li><strong>Is the target a protected path?</strong> Checked before any
        rule, and only for tools whose target is declared to be a filesystem
        path. If so it prompts — or refuses outright where there is nobody to
        ask. <em>An “always allow” you granted earlier cannot reach past
        this.</em></li>
      <li><strong>Is this a shell tool?</strong> Then the command line is taken
        apart and each subcommand is judged on its own, and the most restrictive
        answer governs the whole line.</li>
      <li><strong>Are we in plan mode and is this tool mutating?</strong> Denied
        — though in practice you rarely see this, because plan mode does not
        offer those tools to the model in the first place.</li>
      <li><strong>Do the rules say anything?</strong> <code>deny</code>, then
        <code>ask</code>, then <code>allow</code>, first match wins. Deny beats
        ask beats allow, always, whatever order they were written in.</li>
      <li><strong>Otherwise, the mode decides.</strong> A read-only tool is
        allowed; everything else takes the mode's default.</li>
    </ol>
    ${quote("Two principles: parse, don't prefix-match — decompose compound bash "
      + "commands. Deny beats allow, everywhere.",
      "quickcode/core/permissions.py")}

    ${sub("The five modes")}
    <p class="hp-p">The mode is the default answer for anything that changes
      something. It is shown on the pill beside the composer, changed there or
      with <code>/mode</code>, and it is announced to the model on the next turn
      whenever you change it — the agent is told which mode it is in, so it can
      stop proposing things that would only be denied.</p>
    ${modesHtml()}
    <div id="hp-yolo-slot"></div>
    ${note("Two things people expect that are not true here", `
      <p class="hp-p"><strong>auto-edit does not auto-run shell commands.</strong>
        It auto-allows file edits inside the project. A shell command still
        asks.</p>
      <p class="hp-p"><strong>yolo is not unconditional.</strong> A protected path
        still prompts, and four catastrophic command shapes — <code>rm -rf /</code>,
        <code>rm -rf ~</code>, a force push, and the classic fork bomb — still
        prompt. Yolo also has to be unlocked at launch; if the app was not
        started with <code>--yolo</code> the mode is not offered at all.</p>`)}

    ${sub("Rules: the named exceptions")}
    <p class="hp-p">A rule is either a bare tool name, or a tool name with a
      pattern for the thing it is acting on:</p>
    <dl class="hp-defs">
      <dt class="hp-dt">bash(npm *)</dt>
      <dd class="hp-dd">Any <code>npm</code> command with one more word-run after
        it. The space is literal and required, so this does not match
        <code>npmx</code>.</dd>
      <dt class="hp-dt">edit(src/**)</dt>
      <dd class="hp-dd">Editing anything under <code>src/</code> at any depth.
        <code>**</code> is the only wildcard that crosses a directory
        separator.</dd>
      <dt class="hp-dt">read(.env)</dt>
      <dd class="hp-dd">Exactly the target <code>.env</code> — matching is
        whole-string, so this does not reach <code>config/.env</code>.</dd>
      <dt class="hp-dt">write</dt>
      <dd class="hp-dd">A bare tool name: matches every call to that tool
        whatever its arguments.</dd>
    </dl>
    <p class="hp-p">Two wildcards and nothing else. <code>*</code> matches any run
      of characters <em>except</em> a slash or backslash — including spaces, which
      is why <code>bash(git *)</code> covers <code>git status --short</code>.
      <code>**</code> matches anything at all, separators included. Everything
      else in the pattern is a literal, and the whole target has to match, not
      just the start of it.</p>

    ${sub("Where the rules live")}
    <dl class="hp-defs">
      <dt class="hp-dt">.quickcode/<wbr>settings.json</dt>
      <dd class="hp-dd">The project's rules, under a <code>permissions</code> key
        with <code>allow</code>, <code>ask</code> and <code>deny</code> lists.
        Checked into the repository if you want the team to share them.</dd>
      <dt class="hp-dt">.quickcode/<wbr>settings.local.json</dt>
      <dd class="hp-dd">The same three lists, and where <em>Always allow</em>
        writes what you approved. This is the accreted, personal file — it is
        gitignored, and it is the one to open when you want to take an approval
        back.</dd>
    </dl>
    <p class="hp-p">The two files are merged, and because the <em>kind</em> of
      list is checked before where it came from, it makes no difference which
      file a rule sits in: a <code>deny</code> in either beats an
      <code>allow</code> in either.</p>
    ${note("Subagents do not inherit your rules", `
      <p class="hp-p">A subagent is created with an empty rule set, so an
        <em>always allow</em> you granted in the main conversation does not widen
        what a child may do. A child also has nobody to prompt: an
        <code>ask</code> inside a subagent is refused, with an explanation the
        child can read. What actually governs a child is its permission
        ceiling — the least privileged of its own cap and the mode you are in,
        and <code>plan</code> collapses to <code>ask</code> because a headless
        child cannot hold a plan review.</p>`)}

    ${sub("Protected paths")}
    <p class="hp-p">These sit in front of the rule list, so no accumulated
      approval can quietly reach them. A target is protected when it resolves
      to:</p>
    <ul class="hp-list">
      <li class="hp-li">anywhere <strong>outside the project root</strong>;</li>
      <li class="hp-li">any path with a <code>.git</code> or
        <code>.quickcode</code> component — the repository's own state, and
        QuickCode's;</li>
      <li class="hp-li">any path with an <code>.ssh</code> component, or a
        component that is <code>.env</code> or begins <code>.env.</code>;</li>
      <li class="hp-li">or anything that could not be resolved at all — the check
        fails closed.</li>
    </ul>
    <p class="hp-p">Shell commands get the same treatment: every non-option token
      on the line is treated as a possible path and checked, so a protected file
      cannot be reached by naming it as an argument instead of as a tool
      target.</p>

    ${sub("A tool declares its own shape")}
    <p class="hp-p">The engine holds no list of which tools are dangerous. Each
      tool declares four facts about itself and the gate reads them:</p>
    <dl class="hp-defs">
      <dt class="hp-dt">mutates</dt>
      <dd class="hp-dd">Changes something you would want a say over. Withheld in
        plan mode, prompted for in ask mode.</dd>
      <dt class="hp-dt">target_field</dt>
      <dd class="hp-dd">Which argument carries the thing being acted on — the
        string a rule pattern is matched against.</dd>
      <dt class="hp-dt">path_target</dt>
      <dd class="hp-dd">That target is a filesystem path, so the protected-path
        check applies to it.</dd>
      <dt class="hp-dt">shell</dt>
      <dd class="hp-dd">That target is a command line, so it gets decomposed per
        subcommand.</dd>
    </dl>
    ${quote("The engine used to keep name lists of which tools mutate and which "
      + "argument holds the path. That worked exactly as long as every tool was "
      + "one we shipped: a plugin tool that wrote files got none of the "
      + "protection `write` got, purely because it was not called `write`.",
      "quickcode/core/permissions.py")}
    <p class="hp-p">A tool that declares nothing is treated as mutating with no
      target — prompted for rather than waved through. That default is the reason
      an MCP server you connect cannot accidentally arrive unguarded.</p>

    ${sub("The trust gate is a different question")}
    <p class="hp-p">Permissions ask “may the agent do this?”. Trust asks
      something prior: <strong>may the files in this folder start a process on
      your machine at all?</strong> Opening a project you were sent is enough to
      make that question real, so it is asked before anything runs, once per
      project.</p>
    <p class="hp-p">It covers exactly two things, both project-scope:</p>
    <ul class="hp-list">
      <li class="hp-li">the <code>mcpServers</code> declared in the project's own
        settings files — each one is a command line that would be executed;</li>
      <li class="hp-li">the authored <strong>command tools</strong> in
        <code>.quickcode/plugins/</code> — markdown files that describe a program
        to run.</li>
    </ul>
    <p class="hp-p">Until you approve them, they are <em>inert</em>: declared,
      visible, listed — and not started. Approving records a decision
      <strong>keyed to that project's resolved path, in your home directory</strong>,
      never inside the project. A project cannot declare itself trustworthy,
      because the only place the answer is read from is a file the project cannot
      write.</p>
    <p class="hp-p">The record is bound to a <strong>hash</strong> of exactly what
      you approved: the server definitions themselves, plus the full contents of
      every command-tool file. Edit a server's command, or change one of those
      files, and the hash no longer matches — so it becomes untrusted again and
      asks. Approval is of a specific configuration, not of a folder
      forever.</p>
    ${note("What the gate deliberately does not cover", `
      <p class="hp-p">MCP servers declared at <em>user</em> scope, in your own
        <code>~/.quickcode/settings.json</code>, are not gated. You wrote that
        file; asking you to approve your own configuration every time you open a
        folder would train you to click through the prompt that matters.</p>`)}

    <p class="hp-p">${link("#/help/handson", "Hands-on")} has a sandbox where you
      can type a rule and a call and watch this whole decision run.</p>
  `;

  host.innerHTML = pageHtml("Permissions & trust", {
    crumb: "Help",
    sigil: "§",
    lede: `Two separate gates. <em>Permissions</em> decide whether the agent may
      make a particular change; <em>trust</em> decides whether this project's own
      configuration is allowed to start processes on your machine. This page is
      precise on purpose — it is the part of the app where being approximately
      right is not good enough.`,
    body,
  });

  // Yolo is offered only when the app was launched with --yolo. "It has to be
  // unlocked" is weaker than telling someone whether it is unlocked for them,
  // right now — and the answer is already in the bootstrap this shell booted on,
  // so it costs nothing to say.
  const slot = host.querySelector("#hp-yolo-slot");
  const bs = store.bootstrap;
  if (!slot || !bs) return;
  slot.innerHTML = honesty("live", bs.allow_yolo
    ? "This install was launched with --yolo, so yolo mode is offered in the "
      + "mode menu on this machine."
    : "This install was not launched with --yolo, so yolo mode is not offered "
      + "in the mode menu here — it is described above for completeness.");
}
