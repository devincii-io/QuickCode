// The Hooks page's pure half: what each event is, what the form accepts, and
// how the list is grouped. No DOM, so tests/js/hooks.test.mjs can run it.
//
// The checks mirror `quickcode/hooks/store.py` so a mistake shows while it is
// being typed. The server repeats every one of them and its answer is the one
// that counts: a check that only lived here would be a suggestion.

export const MAX_TIMEOUT_S = 600;
export const DEFAULT_TIMEOUT_S = 30;

/** In the order the loader lists them, with what a hook on each can do. */
export const EVENTS = [
  { name: "PreToolUse", tool: true,
    when: "after the permission engine decides a call, before it runs",
    can: "refuse the call, or demand a prompt — never skip one" },
  { name: "PostToolUse", tool: true,
    when: "after a tool ran and returned a result",
    can: "send the model a note alongside the result" },
  { name: "UserPromptSubmit", tool: false,
    when: "when you send a message, before the model sees it",
    can: "refuse the message, or add context to it" },
  { name: "Stop", tool: false,
    when: "when a turn finishes on its own",
    can: "run (a notifier, say); it cannot block" },
  { name: "SessionStart", tool: false,
    when: "once per session, as its first turn starts",
    can: "add context for the model; it cannot block" },
];

export const STATUS = {
  active: { label: "active", note: "Runs in new sessions." },
  disabled: { label: "switched off", note: "Declared, and switched off on its Settings card." },
  refused: { label: "refused", note: "This project is not trusted, so its hooks are saved "
    + "and never run. Trust the project from the banner in the workspace to let them run." },
};

export function eventInfo(name) {
  return EVENTS.find((e) => e.name === name) || null;
}

export function isToolEvent(name) {
  return !!eventInfo(name)?.tool;
}

// Python's \w is Unicode, so the server takes an MCP tool named in any script.
const ALTERNATIVE = /^[\p{L}\p{M}\p{N}\p{Pc}.:*?[\]!-]+$/u;
const REGEX_HINTS = /\.\*|\.\+|[\\^$()+{}]/;

/** "" when the matcher is usable for this event, else what is wrong with it. */
export function checkMatcher(event, matcher) {
  const m = String(matcher ?? "").trim();
  if (m === "" || m === "*") return "";
  if (!isToolEvent(event)) {
    return `${event} is not about a tool, so a matcher would be ignored. Leave it empty.`;
  }
  for (const raw of m.split("|")) {
    const alt = raw.trim();
    if (!alt) return "An empty alternative (a stray |) matches nothing.";
    if (REGEX_HINTS.test(alt)) {
      return `${alt} reads like a regular expression; matchers are globs `
        + `(write ${alt.replaceAll(".*", "*")}).`;
    }
    if (!ALTERNATIVE.test(alt)) {
      return `${alt} is not a tool name or a glob. Use names and * ? [ ], separated by |.`;
    }
    let depth = 0;
    for (const ch of alt) {
      if (ch === "[") depth += 1;
      if (ch === "]") depth -= 1;
      if (depth !== 0 && depth !== 1) break;
    }
    if (depth !== 0) return `${alt} has an unbalanced [ or ].`;
  }
  return "";
}

/** `{value, error}`: `value` is null for "not stated", which means the default. */
export function checkTimeout(raw) {
  const text = String(raw ?? "").trim();
  if (text === "") return { value: null, error: "" };
  const n = Number(text);
  if (!Number.isFinite(n)) return { value: null, error: "A timeout is a number of seconds." };
  if (n <= 0 || n > MAX_TIMEOUT_S) {
    return { value: null, error: `More than 0 and at most ${MAX_TIMEOUT_S} seconds.` };
  }
  return { value: n, error: "" };
}

export function checkCommand(command) {
  const c = String(command ?? "");
  if (!c.trim()) return "A hook needs a command.";
  if (c.includes("\0")) return "A command cannot contain a NUL character.";
  return "";
}

/** Every problem with a draft, by field; an empty object means it can be sent. */
export function checkDraft({ event, matcher, command, timeout }) {
  const errors = {};
  if (!eventInfo(event)) errors.event = "Pick an event.";
  const m = checkMatcher(event, matcher);
  if (m) errors.matcher = m;
  const c = checkCommand(command);
  if (c) errors.command = c;
  const t = checkTimeout(timeout);
  if (t.error) errors.timeout = t.error;
  return errors;
}

/** The body the server takes, from what the form holds. */
export function draftBody({ event, matcher, command, timeout }) {
  const body = {
    event,
    matcher: isToolEvent(event) ? String(matcher ?? "").trim() : "",
    command: String(command ?? "").trim(),
  };
  const t = checkTimeout(timeout);
  if (t.value != null) body.timeout = t.value;
  return body;
}

/** `[{event, hooks}]` for every event, in order, empty ones included — an
 *  empty group is where the "add one" affordance for that event lives. */
export function groupByEvent(hooks) {
  return EVENTS.map((e) => ({
    event: e, hooks: (hooks || []).filter((h) => h.event === e.name),
  }));
}

export function matcherText(hook) {
  if (!isToolEvent(hook.event)) return "";
  const m = String(hook.matcher || "").trim();
  return m === "" || m === "*" ? "every tool" : m;
}

/** A tool the matcher names outright, for the test panel; else bash. The same
 *  choice `hooks/trial.py:default_tool` makes when none is sent. */
export function defaultTool(matcher) {
  for (const raw of String(matcher || "").split("|")) {
    const alt = raw.trim().toLowerCase();
    if (alt && alt !== "*" && !/[*?[]/.test(alt)) return alt;
  }
  return "bash";
}

/** `{value, error}` for the tool-input box: empty means "use the sample". */
export function parseToolInput(text) {
  const t = String(text ?? "").trim();
  if (!t) return { value: null, error: "" };
  let value;
  try { value = JSON.parse(t); } catch (err) {
    return { value: null, error: `Not JSON: ${err.message}` };
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { value: null, error: "The tool's arguments are a JSON object." };
  }
  return { value, error: "" };
}

/** Where a hook lives, in words. */
export function fileLabel(scope, file) {
  if (scope === "user") return "~/.quickcode/settings.json";
  return `.quickcode/${file || "settings.json"}`;
}

/** The "Saved in" choices, as `scope|file` values. */
export const PLACES = [
  { value: "user|settings.json", scope: "user", file: "settings.json",
    label: "Yours — ~/.quickcode/settings.json, every project on this machine" },
  { value: "project|settings.json", scope: "project", file: "settings.json",
    label: "This project — .quickcode/settings.json, shared with whoever clones it" },
  { value: "project|settings.local.json", scope: "project", file: "settings.local.json",
    label: "This project, this machine — .quickcode/settings.local.json" },
];

export function hookHref(hook) {
  return `#/config/hooks/${encodeURIComponent(hook.id)}?file=${encodeURIComponent(hook.file)}`;
}
