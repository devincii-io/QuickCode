// The permission modes, one sentence each, for every surface that lists them.

export const MODES = [
  ["plan", "Plan mode", "Read-only exploration; the agent submits a plan for your review before touching anything."],
  ["ask", "Ask mode", "Every mutating action (writes, edits, shell) asks for permission first."],
  ["auto-edit", "Auto-edit mode", "File edits inside the project run automatically; shell commands still ask."],
  ["dontask", "Don't-ask mode", "Never prompts — mutating actions outside the allow rules are denied."],
  ["yolo", "Yolo mode", "Skips all permission prompts (requires launching with --yolo)."],
];
