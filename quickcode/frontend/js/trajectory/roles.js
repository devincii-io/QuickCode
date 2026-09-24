// What an event *is*: its role, the lane it draws in, a one-line preview, and
// the configuration page that governs it. Pure — no DOM, no store — so the
// trajectory, the inspector and the chat cards all classify the same way.

import { fmtTokens, oneLine } from "../util.js";

export const ROLES = [
  "SYSTEM", "USER", "CONTEXT", "ASSISTANT", "TOOL", "SUBTOOL", "REVIEW", "AGENT", "ERROR",
];

// Every role lands in exactly one lane; META (usage, mode/model changes,
// compaction) is model bookkeeping. SUBTOOL is a subagent's own tool call, so it
// draws beside the agent that made it rather than among the main agent's tools.
export const LANES = [
  { key: "input", label: "Input", roles: ["USER", "SYSTEM", "CONTEXT", "REVIEW"] },
  { key: "model", label: "Model", roles: ["ASSISTANT", "META", "ERROR"] },
  { key: "tools", label: "Tools", roles: ["TOOL"] },
  { key: "agents", label: "Agents", roles: ["AGENT", "SUBTOOL"] },
];

const LANE_OF = {};
LANES.forEach((l, i) => l.roles.forEach((r) => { LANE_OF[r] = i; }));

export const LANE_TOOLS = 2;
export const LANE_AGENTS = 3;

export function laneOf(role) { return LANE_OF[role] ?? 1; }

/** The event a subagent emitted is wrapped; everything else is its own inner. */
export function innerOf(ev) {
  return ev.type === "agent_event" ? (ev.ev || {}) : ev;
}

export function roleOf(ev) {
  if (ev.type === "agent_event") {
    const t = ev.ev?.type;
    return t === "tool_call" || t === "tool_result" ? "SUBTOOL" : "AGENT";
  }
  switch (ev.type) {
    case "system_prompt": return "SYSTEM";
    case "user_message": return "USER";
    case "context_injection": return "CONTEXT";
    case "assistant_message": return "ASSISTANT";
    case "tool_call": case "tool_result": return "TOOL";
    case "permission_request": case "permission_resolved":
    case "plan_request": case "plan_resolved": return "REVIEW";
    case "agent_spawned": case "agent_done": return "AGENT";
    case "error": return "ERROR";
    default: return "META";
  }
}

export function previewOf(ev) {
  const inner = innerOf(ev);
  switch (inner.type) {
    case "system_prompt": return "System prompt · " + oneLine(inner.text, 160);
    case "user_message": return oneLine(inner.text, 200);
    case "context_injection": return oneLine(inner.text, 200);
    case "assistant_message":
      return oneLine(inner.text || (inner.reasoning ? "(reasoning only)" : ""), 200);
    case "tool_call": return `${inner.name}(${oneLine(inner.arguments, 160)})`;
    case "tool_result": return `${inner.name} → ${oneLine(inner.content, 160)}`;
    case "permission_request": return `ask: ${inner.tool}(${oneLine(inner.arg, 120)})`;
    case "permission_resolved":
      return `${inner.allow ? "allowed" : "denied"}: ${inner.tool}(${oneLine(inner.arg, 110)})`;
    case "plan_request": return "plan submitted for review";
    case "plan_resolved":
      return inner.approved ? `plan approved → ${inner.mode_after}` : "plan revision requested";
    case "mode_changed": return `mode → ${inner.mode}`;
    case "model_changed": return `model → ${inner.model}`;
    case "compacted": return `compacted (${inner.summary_chars} char summary)`;
    case "composition_changed": return compositionPreview(inner);
    case "profile_changed": return profilePreview(inner);
    case "agent_spawned": return `spawned ${ev.agent_id} (${ev.definition})`;
    case "agent_done": return `${ev.agent_id} ${ev.status || "done"}`;
    case "system_note": return oneLine(inner.text, 200);
    case "hook_run":
      return `${inner.event} hook${inner.tool ? ` on ${inner.tool}` : ""} → ${inner.outcome}${
        inner.reason ? ` · ${oneLine(inner.reason, 120)}` : ""}`;
    case "usage":
      return `tokens in ${fmtTokens(inner.input_tokens)} / out ${fmtTokens(inner.output_tokens)}`;
    case "error": return oneLine(inner.message, 200);
    case "worktree": return worktreePreview(inner);
    default: return oneLine(JSON.stringify(inner), 160);
  }
}

// An isolated subagent's checkout: where it went, and where its work ended up.
function worktreePreview(inner) {
  const where = inner.branch ? ` → ${inner.branch}` : "";
  if (inner.action === "committed") {
    return `worktree committed${where} · ${inner.files} file${inner.files === 1 ? "" : "s"} ` +
      `+${inner.insertions} −${inner.deletions}`;
  }
  const why = inner.detail ? ` · ${oneLine(inner.detail, 120)}` : "";
  return `worktree ${inner.action}${where}${why}`;
}

// A composition switch is the single most consequential row in the log — the
// agent's tools, ceiling and delegation all changed under a live conversation.
// The event already carries the answer; this is only the sentence.
function compositionPreview(inner) {
  const n = (inner.tools || []).length;
  const moved = [];
  if (inner.gained?.length) moved.push("+" + inner.gained.join(", "));
  if (inner.lost?.length) moved.push("−" + inner.lost.join(", "));
  const from = inner.from_preset && inner.from_preset !== inner.preset
    ? ` (was ${inner.from_preset})` : "";
  return `composition → ${inner.title || inner.preset}${from} · ${n} tool${
    n === 1 ? "" : "s"} · ceiling ${inner.ceiling}` +
    (moved.length ? " · " + moved.join(" · ") : " · same tools") +
    (inner.spawns?.length ? ` · spawns ${inner.spawns.join(", ")}` : " · no delegation");
}

// A posture switch changes what the next tool call may do without asking, which
// makes it the row that explains every permission event after it.
function profilePreview(inner) {
  if (!inner.profile) return "permission profile cleared";
  return `permission profile → ${inner.title || inner.profile} · mode ${
    inner.mode} · ${inner.allow} allow · ${inner.ask} ask · ${inner.deny} deny`;
}

// ---- cross-links into #/config/… -----------------------------------------
//
// The configuration view is addressed by `#/config/…` and main.js shows it
// from anywhere, so a target is a plain href that survives being copied.

const enc = encodeURIComponent;

function toolTarget(name) {
  return name ? { href: `#/config/parts/tools/${enc("tool." + name)}`, label: `tool.${name}` } : null;
}

function agentTarget(definition) {
  return definition
    ? { href: `#/config/agents/${enc("agent." + definition)}`, label: `agent.${definition}` }
    : null;
}

/** The configuration page that governs one event, or null when nothing does.
 *  `agentDefs` maps agent_id → definition: only `agent_spawned` carries it. */
export function configTarget(ev, inner = innerOf(ev), agentDefs = null) {
  switch (inner.type || ev.type) {
    case "tool_call": case "tool_result":
      return toolTarget(inner.name);
    case "permission_request": case "permission_resolved":
      return toolTarget(inner.tool);
    case "agent_spawned":
      return agentTarget(ev.definition || inner.definition);
    case "composition_changed":
      return inner.preset
        ? { href: `#/config/compositions/${enc(inner.preset)}`, label: inner.preset }
        : null;
    case "profile_changed":
      return inner.profile
        ? { href: `#/config/profiles/${enc(inner.profile)}`, label: inner.profile }
        : { href: "#/config/profiles", label: "profiles" };
    case "system_prompt": case "context_injection":
      return { href: "#/config/parts/prompt", label: "prompt" };
    case "model_changed":
      return { href: "#/config/parts/models", label: "models" };
    case "mode_changed": case "permission_denied":
      return { href: "#/config/parts/policies/runtime.permissions", label: "runtime.permissions" };
    case "compacted":
      return { href: "#/config/parts/policies/runtime.compaction", label: "runtime.compaction" };
    default:
      // Anything a subagent emitted points at the definition it was spawned
      // from, which is the page that decided what it was allowed to do.
      return ev.agent_id ? agentTarget(agentDefs?.get(ev.agent_id)) : null;
  }
}
