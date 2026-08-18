# Redesign requirements (source of truth for the plugin/UI overhaul)

Captured from Devin's feedback + reference screenshots of the DeepSeek Harness.
The DeepSeek *design* is explicitly **not** to be copied — only the technical
model behind it. Ours should end up better.

## 1. Everything internal is a plugin

- Tools, providers, MCP definitions, the agent loop, the shell policy, web
  search, session titling, prompts — all of it is a plugin, including the
  parts we ship ourselves ("internal plugins").
- The **system prompt is a plugin**. Editable, but in tiers:
  - **free** — edit freely (tone, style, project notes).
  - **confirm** — editable, but changing it asks for confirmation first
    (it changes agent behaviour in ways that can break things).
  - **locked** — not changeable at all (e.g. *how tools are called* — the
    tool-call protocol), but **always fully viewable**. Nothing is hidden.
- Agents are plugins too: the `explore` subagent is a plugin, every agent is
  a plugin. Per-agent config must allow:
  - restricting an agent to specific models,
  - offering a selection of models per agent,
  - restricting which tools/capabilities it gets.
- Safe extension points should be addable by the user.

## 2. Agent presets (composition, not a settings dump)

A preset is *the plugin composition a session's agent runs* — its tools, its
prompt, its capabilities. Built-ins to duplicate and make your own; a mode
that helps draft a custom one. Sessions keep the preset they started with;
changing the preset applies to new sessions.

## 3. Plugin UI (current one is not acceptable)

Today Settings → Plugins is a flat list of tool names with `builtin` badges.
Replace with:
- **Plugin configuration** — grouped cards per concern (shell, agent loop,
  web search, prompt, …), expandable, each showing what it actually controls.
- **Plugin list** — searchable, enabled/disabled state per plugin, grouped,
  scales to a large number of plugins.
- Every plugin: fully viewable definition, mutability tier shown, and a way
  to open the underlying configuration file.

## 4. Parsed views everywhere

Every request/response/tool call must be inspectable as structured data, not
only as rendered chat: **Summary / Payload / Result / Timing** tabs, with the
raw JSON (including the full system prompt) readable and syntax-highlighted.

## 5. Trajectory / timeline

- Lanes at the top (Input / Model / Tools) with real duration bars.
- **A pointer/playhead with hover crosshair**, not dots. Hovering shows the
  event kind, its start → end timestamp and total ms.
- Event rows below, aligned to the lanes: `SYSTEM / USER / CONTEXT /
  ASSISTANT / TOOL / SUBTOOL`, each with a left type label, a content preview
  and a `→` result preview, colour-coded per type.
- Must stay usable on long sessions (scrolling, zoom, follow-live).

## 6. Chat rendering

Group tool calls into steps with a title, collapsible `IN` / `OUT` blocks,
syntax highlighting, file links for reads, errors in red inline. Bottom
status bar with real numbers: turns · steps, LLM time, tool time, TTFT,
tok/s, cache hit, input/output tokens.

## 7. Panels

Trajectory / Agents / Tasks / Files / Usage. Agents view needs **good
scrolling and proper multi-agent views** — parallel agents side by side or
stacked with their own call lists, not one flat truncated list.

## 8. Working agreement

- Design each aspect properly first (subagent/design pass), then implement
  aspect by aspect, in order, however long it takes. No shortcuts.
- Don't over-test. Don't write tests before the thing actually works.
