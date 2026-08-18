// The five permission modes, as the engine actually implements them.
//
// One table, read by the Permissions page and by the hands-on mode comparison,
// so the two can never describe different apps. Every cell was checked against
// `PermissionEngine.evaluate` / `_mode_default_for_write` / `_eval_bash_sub` in
// `quickcode/core/permissions.py` and against `PlanModeHook.visible_tools` in
// `quickcode/core/hooks.py`.
//
// Deliberately not imported from js/modals.js: that list is one sentence per
// mode for a dropdown, and stretching it to carry four decision columns would
// make the dropdown worse to make this page possible.
//
// `withholds` is the one column that is not about the *answer* but about the
// *offer*: plan mode is the only mode that changes which tools the model is
// shown at all, and that distinction is the reason plan mode works rather than
// merely being advised.

export const MODES = [
  {
    id: "plan",
    title: "Plan mode",
    what: "Read-only exploration. The agent works out what it would do and "
        + "submits it for your review before anything is touched.",
    write: "denied — and, more to the point, not offered",
    read: "allowed",
    shell: "only the read-only builtins; anything else is denied",
    protected: "prompts",
    withholds: true,
    caveat: "The mutating tools are withheld from the request entirely rather "
          + "than denied per call. A tool the model can see is a tool it will "
          + "try, so offering write and denying every attempt would spend a "
          + "round per attempt and teach the model that the mode is advisory. "
          + "Shell tools stay, because their read-only subcommands are still "
          + "worth having. The plan tool is offered here and nowhere else.",
  },
  {
    id: "ask",
    title: "Ask mode",
    what: "The default. Every mutating action stops and asks you first.",
    write: "prompts",
    read: "allowed",
    shell: "prompts, unless it is a read-only builtin or an allow rule matches",
    protected: "prompts",
    withholds: false,
    caveat: "",
  },
  {
    id: "auto-edit",
    title: "Auto-edit mode",
    what: "File edits inside the project run on their own; shell commands still "
        + "ask.",
    write: "allowed",
    read: "allowed",
    shell: "prompts — this mode does not auto-run commands",
    protected: "prompts",
    withholds: false,
    caveat: "The name promises less than people read into it: it auto-allows the "
          + "mutating tools, and the shell is handled by its own pipeline, which "
          + "still lands on a prompt.",
  },
  {
    id: "dontask",
    title: "Don't-ask mode",
    what: "Never prompts. Anything not covered by an allow rule is refused "
        + "rather than escalated.",
    write: "denied",
    read: "allowed",
    shell: "denied, unless it is a read-only builtin or an allow rule matches",
    protected: "denied outright",
    withholds: false,
    caveat: "The only mode where a protected path is refused instead of "
          + "prompted — there is nobody to prompt, and silently proceeding is "
          + "not the alternative on offer.",
  },
  {
    id: "yolo",
    title: "Yolo mode",
    what: "Skips the prompts. Has to be unlocked at launch with --yolo, and is "
        + "not offered in the mode menu otherwise.",
    write: "allowed",
    read: "allowed",
    shell: "allowed",
    protected: "still prompts",
    withholds: false,
    caveat: "Not unconditional. Protected paths still prompt, and four "
          + "catastrophic command shapes still prompt however the mode is set: "
          + "rm -rf on / or ~, a git push --force, and the fork bomb.",
  },
];

export const MODE_IDS = MODES.map((m) => m.id);

export function modeById(id) {
  return MODES.find((m) => m.id === id) || null;
}
