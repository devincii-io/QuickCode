// What "Always allow" will do for one permission prompt, in words.
//
// The rules are the server's (core/permissions.py suggest_rules): one exact
// rule per part of the call that asked, and a list of the parts no rule can
// cover, each with the step that holds it. Nothing is decided here; this only
// names those steps for a person.

export const SETTINGS_FILE = ".quickcode/settings.local.json";

const KEPT_WHY = {
  protected_path: "touches a protected path, which asks every time",
  circuit_breaker: "matches a circuit breaker, which asks every time",
  ask_rule: "an ask rule matches it",
  substitution: "has a substitution or redirection, which no allow rule covers",
  opaque: "points the program at code no rule has seen",
  wildcard: "contains a *, which a rule cannot spell literally",
  unresolvable_command: "its command word is only finished by the shell",
  nesting_limit: "nests deeper than the permission engine follows",
};

/** `{rules, kept, canSave, empty}` for a `permission_request` event. A request
 *  logged before the list existed carries only `rule_suggestion`. */
export function offerSummary(ev) {
  const rules = Array.isArray(ev?.rules) ? ev.rules
    : ev?.rule_suggestion ? [ev.rule_suggestion] : [];
  const kept = (Array.isArray(ev?.kept) ? ev.kept : []).map((k) => ({
    part: String(k?.part ?? ""),
    why: KEPT_WHY[k?.reason] || String(k?.reason ?? ""),
  }));
  let empty = "";
  if (!rules.length) {
    empty = kept.length
      ? "Always allow has nothing to save: this asks again whatever is saved."
      : ev?.hook_reason
        ? "Always allow has nothing to save: the rules already allow this call, and the hook asks each time."
        : "Always allow has nothing to save for this call.";
  }
  return { rules, kept, canSave: rules.length > 0, empty };
}
