// A faithful browser port of the permission engine, for the hands-on sandbox.
//
// This is the one place in Help where the answer is computed rather than read
// off the backend, and that is a deliberate, uncomfortable choice: there is no
// read-only endpoint that evaluates a rule, and the alternative — a sandbox that
// only *describes* what would happen — teaches nothing. So the rules are ported,
// and the port is kept small enough to stay obviously correct:
//
//   `_rule_matches`   → matchRule
//   `_glob_match`     → globMatch
//   `evaluate`        → evaluate
//   `_eval_bash`      → evalBash
//   `_eval_bash_sub`  → evalBashSub
//   `_protected`      → protectedPath   ← the one approximation, see below
//
// Everything here mirrors `quickcode/core/permissions.py` line for line, with
// the same constants and the same ordering. Two deviations, both surfaced in
// the UI rather than buried here:
//
//   1. **Path resolution.** Python resolves the target against the real project
//      root and consults the filesystem. A browser cannot. This models the
//      component rules that are observable from the string alone — a `.git`,
//      `.quickcode`, `.ssh`, `.env`/`.env.*` component, an absolute path, or a
//      `..` that climbs out. A symlink pointing outside the project is caught by
//      the real engine and not by this one, which is exactly why the widget says
//      so out loud.
//
//   2. **Tool shape.** The engine reads a tool's `PermissionSpec`. That object
//      is not on the wire, but the kernel publishes `metadata.character`, which
//      is computed *from* it and is an exact encoding of the three fields that
//      matter here — so the shape is recovered from live data rather than from
//      a table written into this file. See `characterToSpec`.
//
// The trace this returns is the point of the whole exercise: not just the
// verdict but which check produced it, and which checks never got to run.

// -- constants, copied verbatim ---------------------------------------------

export const READONLY_BUILTINS = new Set([
  "ls", "cat", "pwd", "head", "tail", "wc", "which", "stat", "diff",
  "echo", "cd", "rg", "grep", "tree", "file", "basename", "dirname",
]);

const WRAPPERS = new Set(["timeout", "time", "nice", "nohup"]);
const ENV_ASSIGNMENT = /^\w+=[\s\S]*$/;
const SPLIT = /&&|\|\||\||;|&|\n/;
const COMPOUND_MARKERS = ["$(", "`", ">", "<"];
const CIRCUIT_BREAKERS = [
  /\brm\s+-rf?\s+\/(?:\s|$)/,
  /\brm\s+-rf?\s+~/,
  /git\s+push\s+.*--force/,
  /:\(\)\s*\{/,
];

export const DECISIONS = ["allow", "ask", "deny"];

// -- the tool's declared shape ----------------------------------------------

/** Recover {mutates, pathTarget, shell} from the kernel's `metadata.character`.
 *
 *  `_tool_character` in kernel/manifest.py derives that string from the tool's
 *  real PermissionSpec, and the derivation is injective over the fields the
 *  gate consults, so this inverts it exactly. `read_only` and `internal_write`
 *  collapse to the same shape because the engine cannot tell them apart either:
 *  both are non-mutating with no path target. */
export function characterToSpec(character) {
  switch (character) {
    case "shell":          return { mutates: true, pathTarget: false, shell: true };
    case "file_write":     return { mutates: true, pathTarget: true, shell: false };
    case "mutating":       return { mutates: true, pathTarget: false, shell: false };
    case "file_read":      return { mutates: false, pathTarget: true, shell: false };
    case "read_only":      return { mutates: false, pathTarget: false, shell: false };
    case "internal_write": return { mutates: false, pathTarget: false, shell: false };
    // An unknown character is a tool this build knows about and this page does
    // not. The engine's own fallback for an undeclared tool is "mutating with
    // no target", so that is what an unknown one gets here too.
    default:               return { mutates: true, pathTarget: false, shell: false };
  }
}

// -- rule matching ----------------------------------------------------------

const RULE_RE = /^(\w+)\(([\s\S]*)\)$/;

/** Whole-string glob where only `**` crosses a directory separator. */
export function globMatch(pattern, value) {
  let src = "";
  let i = 0;
  while (i < pattern.length) {
    if (pattern.startsWith("**", i)) { src += ".*"; i += 2; }
    else if (pattern[i] === "*") { src += "[^/\\\\]*"; i += 1; }
    else { src += escapeRe(pattern[i]); i += 1; }
  }
  // Anchored both ends: Python matches this with `re.fullmatch`, and a partial
  // match here would silently widen every rule the user writes. No `s` flag, so
  // `.` excludes newlines exactly as Python's does.
  let re;
  try { re = new RegExp(`^(?:${src})$`); } catch { return false; }
  return re.test(value);
}

function escapeRe(ch) {
  return ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** `bash(npm *)` / `edit(src/**)` / a bare `write`. */
export function matchRule(rule, tool, arg) {
  const m = RULE_RE.exec(rule);
  if (!m) return rule.trim() === tool;
  if (m[1] !== tool) return false;
  return globMatch(m[2], arg);
}

// -- protected paths (modelled; see the header) ------------------------------

export function protectedPath(target) {
  const raw = String(target || "").trim();
  if (!raw) return false;
  const cleaned = raw.replace(/^['"]|['"]$/g, "");
  // An absolute path, a drive letter or a home-relative one leaves the project
  // by construction as far as this model can tell.
  if (/^([/\\]|~|[A-Za-z]:[/\\])/.test(cleaned)) return true;
  const parts = cleaned.split(/[/\\]+/).filter(Boolean);
  let depth = 0;
  for (const part of parts) {
    if (part === ".git" || part === ".quickcode") return true;
    if (part === ".ssh" || part === ".env" || part.startsWith(".env.")) return true;
    if (part === "..") { depth -= 1; if (depth < 0) return true; }
    else if (part !== ".") depth += 1;
  }
  return false;
}

// -- the engine -------------------------------------------------------------

function step(name, hit, why) {
  return { name, hit, why };
}

/**
 * Evaluate one call.
 *
 * @param {object} o
 * @param {string} o.mode      one of the five mode ids
 * @param {string} o.tool      the tool's name, as a rule would spell it
 * @param {object} o.spec      {mutates, pathTarget, shell}
 * @param {string} o.target    the matched argument (a path, a command line, …)
 * @param {object} o.rules     {allow:[], ask:[], deny:[]}
 * @returns {{decision:string, trace:Array}}
 */
export function evaluate({ mode, tool, spec, target, rules }) {
  const trace = [];
  const done = (decision, name, why) => {
    trace.push(step(name, true, why));
    return { decision, trace };
  };

  // 1. Protected paths always prompt, before any rule.
  if (spec.pathTarget) {
    if (protectedPath(target)) {
      return done(mode === "dontask" ? "deny" : "ask", "protected path",
        mode === "dontask"
          ? "The target is protected and this mode never prompts, so it is refused."
          : "The target is protected, so it prompts before any rule is consulted.");
    }
    trace.push(step("protected path", false, "The target is not protected."));
  } else {
    trace.push(step("protected path", "skip",
      "This tool's target is not declared to be a filesystem path."));
  }

  // 2. Shell tools get decomposed and judged per subcommand.
  if (spec.shell) {
    const bash = evalBash(target, mode, rules);
    trace.push(...bash.trace);
    return { decision: bash.decision, trace };
  }
  trace.push(step("shell decomposition", "skip",
    "This tool is not a shell tool."));

  // 3. Plan mode structurally blocks mutation.
  if (mode === "plan" && spec.mutates) {
    return done("deny", "plan mode",
      "Plan mode denies mutating tools — and in a real session it would not have "
      + "offered this tool to the model at all.");
  }
  trace.push(step("plan mode", "skip", mode === "plan"
    ? "In plan mode, but this tool does not mutate."
    : "Not in plan mode."));

  // 4. deny → ask → allow, first match wins.
  for (const kind of DECISIONS.slice().reverse()) {   // deny, ask, allow
    for (const r of rules[kind] || []) {
      if (matchRule(r, tool, target)) {
        return done(kind, `${kind} rule`, `Matched ${r}`);
      }
    }
  }
  trace.push(step("rules", false, "No rule matched this call."));

  // 5. The mode's default.
  if (!spec.mutates) {
    return done("allow", "mode default",
      "The tool declares itself read-only, so it is allowed in every mode.");
  }
  return done(modeDefaultForWrite(mode), "mode default",
    `Nothing named this call, so ${mode} decides.`);
}

export function modeDefaultForWrite(mode) {
  if (mode === "yolo") return "allow";
  if (mode === "auto-edit") return "allow";
  if (mode === "dontask") return "deny";
  return "ask";
}

// -- the bash pipeline ------------------------------------------------------

export function evalBash(command, mode, rules) {
  const line = String(command || "");
  const subs = line.split(SPLIT).map((s) => s.trim()).filter(Boolean);
  const hasSub = COMPOUND_MARKERS.some((m) => line.includes(m));
  const trace = [];
  const decisions = [];

  trace.push(step("shell decomposition", true,
    subs.length > 1
      ? `Split into ${subs.length} subcommands; the most restrictive answer wins.`
      : "One subcommand."));
  if (hasSub) {
    trace.push(step("substitution", true,
      "The line contains a substitution or redirection marker, which disqualifies "
      + "it from the read-only auto-allow and from every allow rule."));
  }

  for (const sub of (subs.length ? subs : [line])) {
    const r = evalBashSub(sub, mode, rules, hasSub);
    decisions.push(r.decision);
    trace.push(step(`“${sub}”`, true, r.why));
  }

  const breaker = CIRCUIT_BREAKERS.find((cb) => cb.test(line));
  if (breaker) {
    decisions.push("ask");
    trace.push(step("circuit breaker", true,
      "This line matches one of the four catastrophic shapes, which prompt even "
      + "in yolo mode."));
  }

  const decision = decisions.includes("deny") ? "deny"
    : decisions.includes("ask") ? "ask" : "allow";
  trace.push(step("most restrictive wins", true,
    `Across the line: ${decision}.`));
  return { decision, trace };
}

function evalBashSub(sub, mode, rules, hasSub) {
  const tokens = sub.split(/\s+/).filter(Boolean);
  let idx = 0;
  let hasEnvPrefix = false;
  while (idx < tokens.length
      && (WRAPPERS.has(tokens[idx]) || ENV_ASSIGNMENT.test(tokens[idx]))) {
    if (ENV_ASSIGNMENT.test(tokens[idx])) hasEnvPrefix = true;
    idx += 1;
  }
  const stripped = tokens.slice(idx).join(" ");
  const first = idx < tokens.length ? tokens[idx].split("/").pop() : "";

  // Every non-option argument is treated as a possible path.
  for (const token of tokens.slice(idx + 1)) {
    if (token.startsWith("-")) continue;
    let candidate = token.includes("=") ? token.split("=").pop() : token;
    candidate = candidate.replace(/^['"{(,)}]+|['"{(,)}]+$/g, "");
    if (candidate && protectedPath(candidate)) {
      return mode === "dontask"
        ? { decision: "deny", why: `${candidate} is a protected path and this mode never prompts.` }
        : { decision: "ask", why: `${candidate} is a protected path, so it prompts.` };
    }
  }

  for (const r of rules.deny || []) {
    if (matchRule(r, "bash", sub) || matchRule(r, "bash", stripped)) {
      return { decision: "deny", why: `Matched the deny rule ${r}.` };
    }
  }

  if (READONLY_BUILTINS.has(first) && !hasSub && !hasEnvPrefix) {
    return { decision: "allow", why: `${first} is a read-only builtin, so it is allowed without a prompt.` };
  }

  if (mode === "plan") {
    return { decision: "deny", why: "Plan mode allows only the read-only builtins." };
  }

  for (const r of rules.ask || []) {
    if (matchRule(r, "bash", sub)) {
      return { decision: "ask", why: `Matched the ask rule ${r}.` };
    }
  }
  if (!hasSub) {
    for (const r of rules.allow || []) {
      if (matchRule(r, "bash", sub)
          || (!hasEnvPrefix && matchRule(r, "bash", stripped))) {
        return { decision: "allow", why: `Matched the allow rule ${r}.` };
      }
    }
  }

  if (mode === "yolo") return { decision: "allow", why: "yolo allows it." };
  if (mode === "dontask") return { decision: "deny", why: "dontask refuses rather than prompting." };
  return { decision: "ask", why: "Nothing named it, so it prompts." };
}

/** What an “always allow” would write, mirroring `suggest_rule`. */
export function suggestRule(tool, spec, target) {
  if (spec.shell) {
    const first = String(target || "").trim().split(/\s+/)[0] || target;
    return `bash(${first} *)`;
  }
  return `${tool}(${target})`;
}
