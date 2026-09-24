// The five permission modes, described once.
//
// Every surface that names a mode reads this table: the mode menu, the `?`
// reference, the composer's /mode completions, the default-mode and
// permission-profile pickers, and the subagent ceiling picker. The Help pages
// add their decision columns on top of it (js/help/modes.js), so a mode cannot
// be described one way in a dropdown and another way in the page that explains
// it.
//
// `summary` is the one sentence a menu shows. `ceiling` is what the mode means
// as a subagent's `mode_cap`; don't-ask has none because the agent editor does
// not offer it as a ceiling.

export const MODES = [
  {
    id: "plan",
    title: "Plan mode",
    summary: "Read-only exploration; the agent submits a plan for your review "
      + "before touching anything.",
    ceiling: "may not write anything at all",
  },
  {
    id: "ask",
    title: "Ask mode",
    summary: "Every mutating action (writes, edits, shell) asks for permission first.",
    ceiling: "stops for permission on anything mutating — but a subagent has nobody "
      + "to ask, so this is where it stops",
  },
  {
    id: "auto-edit",
    title: "Auto-edit mode",
    summary: "File edits inside the project run automatically; shell commands still ask.",
    ceiling: "may edit files without asking; other mutations still stop",
  },
  {
    id: "dontask",
    title: "Don't-ask mode",
    summary: "Never prompts; mutating actions outside the allow rules are denied.",
    ceiling: null,
  },
  {
    id: "yolo",
    title: "Yolo mode",
    summary: "Skips all permission prompts; it has to be armed first (Settings → "
      + "General, or the --yolo launch flag).",
    ceiling: "may do anything the tool pool allows without asking",
  },
];

export const MODE_IDS = MODES.map((m) => m.id);

export function modeById(id) {
  return MODES.find((m) => m.id === id) || null;
}

/** "Ask — every mutating action … first": a mode as one option in a picker. */
export function modeLabel(m) {
  const name = m.title.replace(/ mode$/, "");
  const sentence = m.summary.replace(/\.$/, "");
  return `${name} — ${sentence[0].toLowerCase()}${sentence.slice(1)}`;
}
