// Hooks — your own commands, run at fixed points of a conversation.
//
// Three shapes behind one route, like Permission profiles:
//   #/config/hooks                      every hook, grouped by event
//   #/config/hooks/new?event=&place=    a blank form
//   #/config/hooks/<id>?file=           one hook: its form, and a test run
//
// The settings files stay the truth; this page is an editor for the `hooks`
// block the loader reads (quickcode/hooks/store.py) and a way to try one
// (quickcode/hooks/trial.py). Two things it has to keep visible, because the
// trust gate decides them and a page that hid either would be lying:
//
//   * A project hook in an untrusted project is **refused**: saved, listed,
//     never run — and a test run is a run, so it is not test-run either.
//   * Saving here never trusts a project. It keeps a trusted one trusted, the
//     way every other settings write the app makes does.
//
// Built from DOM nodes rather than markup strings: commands and hook output
// are text somebody else may have written, and a text node cannot be anything
// but text.

import { renderJson } from "../json_view.js";
import { flash } from "../settings/ui.js";
import { h } from "../ui/dom.js";
import { fmtMs } from "../util.js";
import { problemsCardHtml, wireProblems } from "./problems.js";
import {
  EVENTS, PLACES, STATUS, checkDraft, defaultTool, draftBody, eventInfo, fileLabel,
  groupByEvent, hookHref, isToolEvent, matcherText, parseToolInput,
} from "./hooks_model.js";

// A sentence for the page the next render shows, after a save navigates.
let pendingFlash = null;

const detail = (err) => String(err?.message || err).replace(/^\d+:\s*/, "");

function field(label, control, ...notes) {
  const id = control.id;
  return h("div", { class: "set-field" },
    h("label", { for: id || null }, label), control, ...notes);
}

function note(text, cls = "pf-note") {
  return h("div", { class: cls }, text);
}

// ---- the page -------------------------------------------------------------

export async function renderHooks(host, ctx, selected = "", query = {}) {
  const inner = h("div", { class: "cfg-page-inner" },
    h("div", { class: "set-loading" }, "Reading the hooks…"));
  host.replaceChildren(inner);

  let data;
  try {
    data = await ctx.api.hooks();
  } catch (err) {
    inner.replaceChildren(h("div", { class: "set-error" },
      `Could not read the hooks: ${detail(err)}`));
    return;
  }

  const hook = selected && selected !== "new"
    ? (data.hooks.find((x) => x.id === selected && (!query.file || x.file === query.file))
       || data.hooks.find((x) => x.id === selected))
    : null;
  if (selected && selected !== "new" && !hook) {
    inner.replaceChildren(header(data, "missing"), h("div", { class: "set-error" },
      "There is no such hook any more — it may have been edited outside the app. ",
      h("a", { href: "#/config/hooks" }, "Back to the list")));
    return;
  }

  const pageFlash = h("span", { class: "set-flash", "data-page-flash": true });
  if (selected === "new" || hook) {
    // The form carries its own trust note, beside the choice it is about.
    inner.replaceChildren(header(data, hook ? `${hook.event} hook` : "new"), pageFlash,
      editor(ctx, data, hook, query));
  } else {
    inner.replaceChildren(
      header(data, ""),
      h("div", { class: "cfg-lede" },
        "Your own commands, run at fixed points of a conversation: before and after a "
        + "tool call, on each message, when a turn ends, when a session starts. A hook "
        + "can refuse a call or demand a prompt; it can never skip one. What you save "
        + "here is written to the settings file named beside each hook, and reaches "
        + "new sessions."),
      trustNote(data),
      problemsSlot(data, ctx),
      pageFlash,
      list(ctx, data));
  }
  if (pendingFlash) {
    flash(pageFlash, pendingFlash.text, pendingFlash.kind);
    pendingFlash = null;
  }
}

function header(data, crumb) {
  return h("header", { class: "cfg-head" },
    h("div", { class: "cfg-crumbs" },
      crumb ? [h("a", { href: "#/config/hooks" }, "Hooks"), ` ▸ ${crumb}`] : "Hooks"),
    h("div", { class: "cfg-head-main" },
      h("span", { class: "k-sigil big", "data-kind": "hook" }, "()"),
      h("h2", {}, "Hooks"),
      h("span", { class: "cfg-count" }, String(data.hooks.length)),
      h("span", { class: "cfg-head-actions" },
        h("a", { class: "btn", href: "#/config/hooks/new" }, "+ New hook"))));
}

function trustNote(data) {
  const mine = data.hooks.filter((x) => x.scope === "project").length;
  if (data.trust.trusted) {
    if (!mine) return null;
    return h("div", { class: "hk-trust", "data-trusted": "true" },
      h("b", {}, "Trusted. "),
      "This project's hooks run, and a hook you save to it here keeps it trusted.");
  }
  return h("div", { class: "hk-trust", "data-trusted": "false" },
    h("b", {}, "Not trusted. "),
    mine
      ? `This project declares ${mine} ${mine === 1 ? "hook" : "hooks"}, listed below as `
        + "refused: saved, never run, and not test-run either. "
      : "",
    "A hook you save to this project is kept the same way — saving never trusts a "
    + "project. Your own hooks run everywhere. To let the project's run, read them "
    + "in the trust banner in the workspace and trust the project there.");
}

function problemsSlot(data, ctx) {
  const slot = h("div", { class: "pb-slot" });
  // The refusal is the trust note's to say, on this page; the card is for
  // entries that were written wrong.
  const problems = (data.problems || []).filter((p) => p.code !== "hook_refused");
  // The shared card escapes everything it renders; it is the one markup string
  // on this page, so the Problems page and this one draw a problem the same way.
  slot.innerHTML = problemsCardHtml(problems, {
    title: "Hooks that do not run as written",
    note: "An entry with an error is skipped; the rest of its block still runs.",
  });
  wireProblems(slot, ctx);
  return slot;
}

// ---- the list -------------------------------------------------------------

function list(ctx, data) {
  return h("div", { class: "hk-groups" }, groupByEvent(data.hooks).map(({ event, hooks }) =>
    h("section", { class: "cfg-sec hk-event", "data-event": event.name },
      h("div", { class: "hk-event-head" },
        h("h3", {}, event.name),
        h("span", { class: "hk-event-when" }, `${event.when} — ${event.can}`),
        h("a", { class: "ghost-btn", href: `#/config/hooks/new?event=${event.name}` },
          "+ Add")),
      hooks.length
        ? h("div", { class: "k-list" }, hooks.map((x) => card(ctx, x)))
        : h("div", { class: "hk-none" }, `No ${event.name} hooks.`))));
}

function badges(hook) {
  const place = hook.scope === "user" ? "user"
    : hook.file === "settings.local.json" ? "project · local" : "project";
  return h("span", { class: "k-badges" },
    h("span", { class: "pf-layer", "data-layer": hook.scope, title: hook.path }, place),
    h("span", { class: "hk-status", "data-status": hook.status,
      title: STATUS[hook.status]?.note || "" }, STATUS[hook.status]?.label || hook.status));
}

function facts(hook) {
  const matcher = matcherText(hook);
  return h("div", { class: "k-facts" },
    matcher ? h("span", { class: "k-fact mono", title: "Which tools it sees" },
      `matches ${matcher}`) : null,
    h("span", { class: "k-fact" }, `timeout ${hook.timeout} s`),
    h("span", { class: "k-fact mono", title: hook.path }, fileLabel(hook.scope, hook.file)));
}

function card(ctx, hook) {
  const slot = h("div", { class: "hk-test-slot" });
  const refused = hook.status === "refused";
  const test = h("button", {
    class: "btn", "data-test": true, disabled: refused,
    title: refused ? "This project is not trusted, so its hooks do not run — a test "
      + "run included." : "Run it once now with a sample payload",
  }, "Test");
  test.addEventListener("click", () => {
    if (slot.firstChild) {
      slot.replaceChildren();
      test.textContent = "Test";
      return;
    }
    slot.replaceChildren(testPanel(ctx, hook));
    test.textContent = "Close test";
  });
  const del = h("button", { class: "ghost-btn danger", "data-delete": true }, "Delete");
  del.addEventListener("click", () => remove(ctx, hook, del));

  return h("article", { class: "k-card hk-card", "data-kind": "hook",
    "data-status": hook.status, "data-id": hook.id },
    h("div", { class: "k-card-main" },
      h("div", { class: "k-card-head" },
        h("span", { class: "k-sigil", "data-kind": "hook" }, "()"),
        h("code", { class: "hk-command" }, hook.command),
        badges(hook)),
      facts(hook),
      hook.status !== "active"
        ? h("div", { class: "pf-reduced" }, STATUS[hook.status]?.note || "") : null,
      slot),
    h("div", { class: "k-card-side" },
      test,
      h("a", { class: "ghost-btn", href: hookHref(hook) }, "Edit"),
      del));
}

async function remove(ctx, hook, btn) {
  if (!window.confirm(`Delete this ${hook.event} hook from ${fileLabel(hook.scope, hook.file)}?`
      + `\n\n${hook.command}`)) return;
  btn.disabled = true;
  try {
    await ctx.api.deleteHook(hook.id, hook.file);
  } catch (err) {
    btn.disabled = false;
    pendingFlash = { text: detail(err), kind: "err" };
    ctx.go("#/config/hooks");
    return;
  }
  pendingFlash = { text: "Deleted. New sessions no longer run it.", kind: "ok" };
  // Settings lists each hook as a plugin, so the cached kernel snapshot is now
  // a description of a configuration that no longer exists.
  ctx.invalidate?.();
  ctx.go("#/config/hooks");
}

// ---- the form -------------------------------------------------------------

function editor(ctx, data, hook, query) {
  const isNew = !hook;
  const startEvent = hook?.event || (eventInfo(query.event) ? query.event : "PreToolUse");
  const flashNode = h("span", { class: "set-flash" });

  const event = h("select", { id: "hk-event" },
    EVENTS.map((e) => h("option", { value: e.name, selected: e.name === startEvent }, e.name)));
  const eventNote = note("");
  const matcher = h("input", { id: "hk-matcher", spellcheck: "false", autocomplete: "off",
    placeholder: "bash · write|edit · mcp__*", value: hook?.matcher || "" });
  const matcherBad = note("", "pf-bad");
  const matcherField = field("Matcher", matcher,
    note("Which tools it sees. Empty or * means every tool. Tool names and globs, "
      + "separated by | — globs, not regular expressions, and case does not matter."),
    matcherBad);
  const command = h("textarea", { id: "hk-command", class: "hk-command-input", rows: "3",
    spellcheck: "false", placeholder: "python ~/.quickcode/guard.py" });
  command.value = hook?.command || "";
  const commandBad = note("", "pf-bad");
  const timeout = h("input", { id: "hk-timeout", type: "number", min: "1",
    max: String(data.timeout.max), step: "any", placeholder: String(data.timeout.default),
    value: hook && hook.timeout !== data.timeout.default ? String(hook.timeout) : "" });
  const timeoutBad = note("", "pf-bad");

  const startPlace = PLACES.some((p) => p.value === query.place) ? query.place : PLACES[0].value;
  const place = isNew
    ? h("select", { id: "hk-place" }, PLACES.map((p) =>
      h("option", { value: p.value, selected: p.value === startPlace }, p.label)))
    : h("code", { class: "hk-where", title: hook.path }, hook.path);
  const placeNote = note("", "pf-warn");

  const save = h("button", { class: "btn primary" }, isNew ? "Save hook" : "Save changes");
  const actions = h("div", { class: "f-actions" },
    save, h("a", { class: "ghost-btn", href: "#/config/hooks" }, "Cancel"));
  if (!isNew) {
    const del = h("button", { class: "ghost-btn danger" }, "Delete");
    del.addEventListener("click", () => remove(ctx, hook, del));
    actions.append(del);
  }
  actions.append(flashNode);

  const values = () => ({
    event: event.value, matcher: matcher.value, command: command.value,
    timeout: timeout.value,
  });

  const projectPlace = () => (isNew ? place.value.startsWith("project|") : hook.scope === "project");

  const repaint = () => {
    const info = eventInfo(event.value);
    eventNote.textContent = info ? `Runs ${info.when}. It can ${info.can}.` : "";
    matcherField.hidden = !isToolEvent(event.value);
    const errors = checkDraft(values());
    // An empty command on a blank form is not a mistake yet: Save waits for it
    // without scolding anyone for not having typed.
    const shown = { ...errors };
    if (!commandTouched) delete shown.command;
    for (const [node, key] of [[matcherBad, "matcher"], [commandBad, "command"],
      [timeoutBad, "timeout"]]) {
      node.textContent = shown[key] || "";
      node.classList.toggle("is-bad", !!shown[key]);
    }
    save.disabled = Object.keys(errors).length > 0;
    const untrusted = projectPlace() && !data.trust.trusted;
    placeNote.hidden = !untrusted;
    placeNote.textContent = untrusted
      ? "This project is not trusted. A hook saved to it is kept and listed as refused, "
        + "and it does not run — here or in a session — until you trust the project. "
        + "Saving it does not trust it."
      : "";
  };

  let commandTouched = !isNew;
  command.addEventListener("blur", () => { commandTouched = true; repaint(); });
  for (const node of [event, matcher, command, timeout, ...(isNew ? [place] : [])]) {
    node.addEventListener("input", repaint);
    node.addEventListener("change", repaint);
  }

  save.addEventListener("click", async () => {
    const body = draftBody(values());
    save.disabled = true;
    let res;
    try {
      if (isNew) {
        const chosen = PLACES.find((p) => p.value === place.value) || PLACES[0];
        res = await ctx.api.addHook({ ...body, scope: chosen.scope, file: chosen.file });
      } else {
        res = await ctx.api.updateHook(hook.id, { ...body, file: hook.file });
      }
    } catch (err) {
      save.disabled = false;
      flash(flashNode, detail(err), "err");
      return;
    }
    const saved = res.hook;
    pendingFlash = saved?.status === "refused"
      ? { text: "Saved, and refused: this project is not trusted, so it does not run.",
        kind: "err" }
      : { text: "Saved. New sessions run it.", kind: "ok" };
    ctx.invalidate?.();
    // Onto the saved hook's own page, where the test run is: a changed command
    // has a new id, and "save, then try it" is the order this is used in.
    ctx.go(saved ? hookHref(saved) : "#/config/hooks");
  });

  repaint();

  const form = h("section", { class: "cfg-sec pf-editor hk-editor" },
    h("h3", {}, isNew ? "New hook" : `Editing a ${hook.event} hook`),
    field("Event", event, eventNote),
    matcherField,
    field("Command", command,
      note("Run in the project directory by the shell the bash tool uses — "
        + "/bin/bash -lc on Linux and macOS, Git Bash (or PowerShell) on Windows. It "
        + "reads a JSON payload on stdin; exit 0 is fine, exit 2 blocks with stderr as "
        + "the reason, anything else is a failure that fails open."),
      commandBad),
    field("Timeout (seconds)", timeout,
      note(`Default ${data.timeout.default}, at most ${data.timeout.max}. At the deadline `
        + "the hook's whole process tree is stopped and the hook fails open."),
      timeoutBad),
    isNew
      ? field("Saved in", place, placeNote)
      : h("div", { class: "set-field" }, h("label", {}, "Saved in"), place,
        note("To move it, add it where you want it and delete this one."), placeNote),
    actions);

  if (isNew) return form;
  return h("div", {}, form,
    h("section", { class: "cfg-sec hk-test-sec" },
      h("h3", {}, "Test run"),
      note("Runs the saved hook — save first to try a change — once, now, with a sample "
        + "payload. Nothing reaches a session or a model; the command itself does run."),
      testPanel(ctx, hook)));
}

// ---- the test run ---------------------------------------------------------

function testPanel(ctx, hook) {
  if (hook.status === "refused") {
    return h("div", { class: "pf-reduced hk-refused" },
      "This project is not trusted, so its hooks do not run — and a test run is a run. "
      + "Trust the project from the banner in the workspace to try it.");
  }
  const wrap = h("div", { class: "hk-test" });

  const event = h("select", {}, EVENTS.map((e) =>
    h("option", { value: e.name, selected: e.name === hook.event }, e.name)));
  const tool = h("input", { spellcheck: "false", autocomplete: "off",
    value: defaultTool(hook.matcher) });
  const input = h("textarea", { class: "hk-json", rows: "3", spellcheck: "false",
    placeholder: "Empty: a sample shaped like the tool's own arguments. Or a JSON object, "
      + 'e.g. {"command": "rm -rf build"}' });
  const inputBad = note("", "pf-bad");
  const prompt = h("input", { spellcheck: "false", placeholder: "Hello from a test run…" });
  const fields = h("div", { class: "hk-test-fields" });
  const run = h("button", { class: "btn primary" }, "Run once");
  const result = h("div", { class: "hk-result", "aria-live": "polite" });

  const label = (text, control) => h("label", { class: "hk-test-field" },
    h("span", {}, text), control);

  const paintFields = () => {
    const name = event.value;
    if (isToolEvent(name)) {
      fields.replaceChildren(label("Tool", tool), label("Arguments (JSON)", input), inputBad);
    } else if (name === "UserPromptSubmit") {
      fields.replaceChildren(label("Message", prompt));
    } else {
      fields.replaceChildren(note(name === "Stop"
        ? "Sent: the common fields, plus the turn's last answer (a stand-in)."
        : "Sent: the common fields, plus source: startup."));
    }
  };
  event.addEventListener("change", paintFields);
  input.addEventListener("input", () => {
    const { error } = parseToolInput(input.value);
    inputBad.textContent = error;
    inputBad.classList.toggle("is-bad", !!error);
  });
  paintFields();

  run.addEventListener("click", async () => {
    const body = { event: event.value, file: hook.file };
    if (isToolEvent(event.value)) {
      const parsed = parseToolInput(input.value);
      if (parsed.error) return;
      body.tool_name = tool.value.trim();
      if (parsed.value) body.tool_input = parsed.value;
    } else if (event.value === "UserPromptSubmit" && prompt.value.trim()) {
      body.prompt = prompt.value;
    }
    run.disabled = true;
    run.textContent = "Running…";
    result.replaceChildren(note(`Waiting for the hook — at most ${hook.timeout} s.`));
    try {
      result.replaceChildren(resultView(await ctx.api.testHook(hook.id, body)));
    } catch (err) {
      result.replaceChildren(h("div", { class: "set-error" }, detail(err)));
    } finally {
      run.disabled = false;
      run.textContent = "Run once";
    }
  });

  wrap.append(
    h("div", { class: "hk-test-row" }, label("Event", event), fields),
    h("div", { class: "hk-test-actions" }, run,
      h("span", { class: "pf-note" },
        `In the project directory, with the hook's own timeout (${hook.timeout} s).`)),
    result);
  return wrap;
}

function stream(name, text, cut) {
  const size = `${text.length.toLocaleString()} characters${cut ? ", cut" : ""}`;
  return h("details", { class: "hk-stream", open: !!text },
    h("summary", {}, `${name} `, h("span", { class: "k-dim" }, text ? size : "empty")),
    text ? h("pre", {}, text) : null);
}

function resultView(out) {
  const v = out.verdict;
  const payload = h("pre", {});
  renderJson(payload, JSON.stringify(out.payload, null, 2));
  const said = (title, text) => text
    ? h("div", { class: "hk-said" }, h("b", {}, title), h("pre", {}, text)) : null;
  return h("div", {},
    h("div", { class: "hk-outcome", "data-outcome": v.outcome },
      h("span", { class: "hk-outcome-word" }, v.outcome),
      h("span", { class: "hk-effect" }, out.effect)),
    out.note ? h("div", { class: "pf-warn" }, out.note) : null,
    h("div", { class: "k-facts hk-facts" },
      h("span", { class: "k-fact mono" },
        out.exit_code == null ? "no exit code" : `exit ${out.exit_code}`),
      h("span", { class: "k-fact" }, fmtMs(out.ms) || "0 ms"),
      v.decision ? h("span", { class: "k-fact mono" }, `decision: ${v.decision}`) : null,
      out.timed_out ? h("span", { class: "k-fact" }, "timed out") : null,
      out.spawn_error ? h("span", { class: "k-fact" }, "could not start") : null),
    said("Reason", v.reason),
    said("Context for the model", v.context),
    said("Message for you", v.message),
    stream("stdout", out.stdout, out.stdout_truncated),
    stream("stderr", out.stderr, out.stderr_truncated),
    h("details", { class: "hk-stream" },
      h("summary", {}, "Payload sent on stdin"), payload));
}
