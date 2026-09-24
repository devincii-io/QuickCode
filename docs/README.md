# QuickCode documentation

Start with the repository [README](../README.md) for what QuickCode is and how
to install it, and the root [AGENTS.md](../AGENTS.md) for the conventions and
invariants that bind anyone editing this repository.

## Reference

Describe the code as it is. `tests/test_docs_accuracy.py` checks their tables,
quoted prompts and tool descriptions, file paths and links against the real
objects, so drift fails the test suite.

| Document | Covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Stack, layers, repository layout, the plugin kernel, the agent loop, providers, the bash tool and PTYs, the session event log, the trust boundary |
| [PERMISSIONS.md](PERMISSIONS.md) | Permission modes, rule syntax and precedence, protected paths, circuit breakers, the trust gate for project settings, plan mode, headless runs |
| [HOOKS.md](HOOKS.md) | Command hooks: scripts run before/after tool calls, on each message, at turn end and session start; trust gating for project hooks |
| [TOOLS.md](TOOLS.md) | Every built-in tool: its description as the model sees it, input schema, limits and safety rules; the web tools and search providers |
| [PROMPTS.md](PROMPTS.md) | The system prompt section by section, `<system-reminder>` injection, the compaction prompt |
| [UI.md](UI.md) | The web UI: workspace shell and agent panes, views, trajectory, the WebSocket event protocol, dialogs, keyboard |
| [AGENTS.md](AGENTS.md) | Subagents, background jobs and the task board as a product feature, plus the teammate-mode design that is not built yet (not to be confused with the root `AGENTS.md`) |
| [COMPLIANCE.md](COMPLIANCE.md) | Security and compliance disclosure: network traffic, files written, licences, supply chain, known gaps. A dated audit — its header says which version it covers |
| [ROADMAP.md](ROADMAP.md) | What has shipped, what is in progress, what is next |

## Design rationale

Written before the features they describe were built; kept because they
explain *why*. Each opens with a note on what shipped and how it differs.

| Document | Covers |
|---|---|
| [design/AUTHORING.md](design/AUTHORING.md) | Writing plugins as files: command tools, agents and prompt sections, ids, validation, the trust gate |
| [design/BINDING.md](design/BINDING.md) | Compositions: how a capability is resolved for an agent, why intersection, the pool-versus-grant split |
| [design/UX.md](design/UX.md) | Configuration as a view: the visual grammar and the questions every plugin answers |

## Archive

Completed plans and handoff notes, kept as a record. Each opens with a status
line; none of them describes current behaviour.

| Document | Was |
|---|---|
| [archive/REDESIGN-REQUIREMENTS.md](archive/REDESIGN-REQUIREMENTS.md) | Requirements for the plugin and configuration-UI overhaul (2.0.0) |
| [archive/PLAN-PLUGIN-UI-OVERHAUL.md](archive/PLAN-PLUGIN-UI-OVERHAUL.md) | The plan for that overhaul (2.0.0) |
| [archive/PLAN-AUTHORING-AND-COMPOSITION.md](archive/PLAN-AUTHORING-AND-COMPOSITION.md) | The plan reconciling the three design documents (2.0.0) |
| [archive/PHASE1-HANDOFF.md](archive/PHASE1-HANDOFF.md) | Phase 1 handoff: compositions, resolution and provenance in the kernel |
| [archive/PHASE2-HANDOFF.md](archive/PHASE2-HANDOFF.md) | Phase 2 handoff: the explanation layer |
| [archive/PHASE3-HANDOFF.md](archive/PHASE3-HANDOFF.md) | Phase 3 handoff: configuration as a top-level view |
| [archive/TRUST-HANDOFF.md](archive/TRUST-HANDOFF.md) | Frontend handoff for the project trust gate |
| [archive/UI-TEXTUAL.md](archive/UI-TEXTUAL.md) | UI design for the Textual TUI replaced in 1.0.0 |

## Keeping these honest

- A new document goes in one of the three tables above; the docs test fails
  on a file under `docs/` that this index does not link.
- A reference document states a structure the tests can read — a table, a
  fenced block, a backticked path — rather than a sentence they would have to
  grep. See the module docstring of `tests/test_docs_accuracy.py`.
- When a plan is done, move it to `archive/` with a one-line
  `> **Archived.** …` status and update every link to it (code comments
  included).
