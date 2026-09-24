# Phase 3 handoff — configuration as a top-level view

What landed, what the frontend needs from the backend that does not exist yet,
and what the next passes should pick up. Written against
`docs/PLAN-AUTHORING-AND-COMPOSITION.md` §4 Phase 3 and `docs/design/UX.md`.

> **Status of "what the backend still owes the view" — verified against the
> tree on 2026-09-24.** Everything listed below as owed has landed; the rest of
> this document is the original handoff, kept as the record.
>
> | Owed item | Status | Where (tests) |
> |---|---|---|
> | Explanation fields on `plugin_json` | Landed | `kernel/registry.py` |
> | `SettingSpec.affects`/`effect_detail`/`example`, `docs_anchor` rendered | Landed 2026-09-24 | `js/settings/fields.js`, `js/config/explain.js` |
> | Disabled Duplicate "arrives in the next pass" | Gone — the button is Edit file, Duplicate, or a refusal with its reason and recourse (`explain.js:dupHtml`, `kinds.js:duplicateRefusal`) | |
> | Recourse buttons | Landed; `settings` recourses now open the named plugin's page (they used to land on the Tools list) | `kinds.js:recourseHref` (`tests/js/config_routes.test.mjs`) |
> | `GET /api/kernel/agents` | Landed (in `server/agents_api.py`, not `kernel_api.py`) | `tests/test_workbench.py` |
> | `GET /api/kernel/agents/{id}/resolved` | Landed; `?conv=` for a subagent resolves against the session's snapshot since 2026-09-24 | `test_workbench.py`, `test_kernel_resolution.py` |
> | `POST /api/kernel/agents/{id}/preview` | Landed | `test_workbench.py` |
> | `GET /api/prompt?conv=` frozen vs live | Landed 2026-09-24 — `server/prompt_api.py`; live now resolves the active composition; the Prompt page has a Next session / This session switch and the workbench a This session / Show live link | `tests/test_kernel_api.py` |
> | Authored CRUD (`/api/kernel/authored…`, validate, dry-run) | Landed | `server/authoring_api.py` (`tests/test_authoring.py`) |
> | `POST /api/kernel/plugins/{id}/duplicate` | Landed; agents from `.quickcode/agents/` duplicate too since 2026-09-24 | `test_authoring.py`, `test_kernel_api.py` |
> | `#/config/new/{agent,tool,prompt}` | Landed — create a real file and open the editor | `js/config/create/` |
> | `#/config/new/composition` | Landed 2026-09-24 — a named copy of an existing composition | `js/config/create/composition.js`, `test_kernel_api.py` |
> | `used_by` + USED BY block | Landed | `registry.used_by`, `js/config/usedby.js` (`tests/test_usedby.py`) |
> | Provider model count / endpoint | Landed 2026-09-24 — `metadata.endpoint` (credentials stripped), `metadata.model_count` | `kernel/facts.py`, `test_kernel_api.py` |
> | Tool `signature` on the payload | Landed 2026-09-24 — `metadata.signature`; the per-tool fetch is a fallback | `test_kernel_api.py` |
> | Tools badging `locked` | Resolved — `SettingSpec.fact` keeps `read_only` out of `tier()` | `kernel/spec.py` |
> | Delete `js/settings/{index,plugins,presets,prompt}.js` | Done — only `fields.js`, `general.js`, `ui.js` remain | |
> | Live WebSocket surviving the config switch | Still not verified in a browser | |

## What landed

- `#view-config` is a real third view, peer of `#view-home` and
  `#view-workspace` (`index.html`, `css/shell.css`). `showConfig()` in
  `js/main.js` **never calls `disconnect()`** — the workspace stays mounted and
  hidden with its socket and transcript intact.
- Hash routing: `#/config/agents/<id>`, `#/config/compositions/<id>`,
  `#/config/parts/<slug>[/<plugin.id>]`, `#/config/machine-room`,
  `#/config/install[/<tab>]`, `#/config/new/<kind>`. A `#/config/…` URL in the
  address bar at load opens the view directly.
- Rail: Agents → Compositions → Parts → Machine room → Install.
- The flat 37-row plugin list is retired as a page; the header search box
  queries plugins, their settings and compositions, and lands on the canonical
  page with the matched setting highlighted.
- Machine room is a filter over locked runtime internals plus an index of
  locked settings that live on plugins elsewhere. Every plugin keeps exactly
  one canonical page: `#/config/machine-room/<id>` redirects to it.
- Per-kind visual language in `css/config.css`: nine `--kind-*` tokens (theme
  tokens or `color-mix` of them), two-character ASCII/Latin-1 sigil tiles, and
  the stripe rule — kind owns the hue, tier owns the badge and the stripe
  texture (solid / notched / 45° hatch). Locked never uses `--error`.
- Locked settings render as "Fixed by design": the value legible and
  selectable, the reason, and a recourse row that always ends in something the
  user can do. No disabled inputs anywhere.
- `openSettings()` is gone; `openQuickSettings()` (endpoint · API key · theme ·
  "Open full configuration →") is on the composer as the `⚙ quick` pill.

`js/settings/ui.js` and `js/settings/fields.js` are reused verbatim: the sheet,
`confirmRisk`, `highlightJson`, the `PluginView` inspector and the
403 / 409-with-reason / 200 tier state machine are unchanged.

## What the backend still owes the view

Phase 2 wrote these fields onto `PluginSpec`/`SettingSpec` and filled them in
`manifest.py`, but `kernel/registry.py:plugin_json()` does not serialise them
yet (see `docs/PHASE2-HANDOFF.md` §1), so the payload the browser receives is
still the pre-Phase-2 shape. **Nothing needs to change in the frontend when
that lands** — `js/config/explain.js` already reads every field by name and
falls back only when it is absent:

| field | today's fallback |
|---|---|
| `PluginSpec.summary` | first sentence of `description` |
| `PluginSpec.affects` | inferred from `kind`, marked "inferred" on hover |
| `PluginSpec.audience` | inferred from `kind`, same marking |
| `PluginSpec.consequence` | the IF CHANGED row is omitted rather than invented |
| `PluginSpec.locked_because` | a per-kind neutral sentence in `js/config/explain.js` |
| `PluginSpec.recourse` | a disabled Duplicate button + "Read the full definition" |
| `PluginSpec.docs_anchor` | no docs link is offered |
| `SettingSpec.affects` / `effect_detail` / `example` | not rendered |

Phase 4 (`server/kernel_api.py`):

- `GET /api/kernel/agents` — the rail currently derives the agent list from
  `kind == "agent"` plugins plus a synthetic `@orchestrator` entry.
- `GET /api/kernel/agents/{id}/resolved` — the workbench placeholder on every
  agent page names this. Without it the view cannot show an agent's composed
  prompt, its section boundaries, its resolved tool list or its denied tools.
- `POST /api/kernel/agents/{id}/preview` — the live draft preview.
- `GET /api/prompt?conv=` — the frozen/live distinction is not renderable yet;
  the Prompt page says "the prompt the next session starts from", which is
  true today because it always re-resolves.

Phase 5/6 (authoring):

- `GET|POST /api/kernel/authored…`, `POST /api/kernel/plugins/{id}/duplicate`.
  The Duplicate button exists at every locked tier and is disabled with a title
  saying it arrives in the next pass; `#/config/new/{agent,tool,prompt,composition}`
  are reachable pages that describe what will be written and say plainly that
  there is nowhere to write it yet.

Two smaller things the view would use immediately:

- `plugin_json` `used_by` (Phase 7) — the USED BY block is not rendered at all.
- A provider's model count / endpoint: the provider card reads the endpoint
  from `/api/bootstrap` and does not show a model count, because
  `provider_specs()` carries neither.

## Notes for whoever picks this up

- `PluginSpec.tier()` is the strictest tier among a plugin's settings, so every
  **tool** comes back `locked` (its only setting is the declared `read_only`
  flag). The Machine room therefore filters locked plugins by kind
  (`policy|hook|storage|panel`) rather than by tier alone — otherwise it would
  hold 21 entries, 13 of them tools. If Phase 2 gives tools a `tier_hint` or a
  free setting, revisit `MACHINE_KINDS` in `js/config/machineroom.js`.
- `js/settings/{index,plugins,presets,prompt}.js` are no longer reachable from
  anywhere: the Settings modal that mounted them is gone. They were left on
  disk rather than deleted because Phase 2's file list includes
  `js/settings/plugins.js`; delete all four — and the `.plug-card` / `.plug-row`
  rules in `css/settings.css` — once that edit has landed. `fields.js`,
  `ui.js` and `general.js` are all still in use.
- Tool signatures on the Tools page come from one `GET /api/kernel/plugins/{id}`
  per tool, cached for the life of the view. A `signature` (or the parameter
  names) on the plugin payload would remove 13 requests.

## Verified in a browser (dev server on :8834, Playwright)

Every rail destination renders; search finds a setting and highlights it on the
page it lands on; a `free` bool and a `free` prompt body save (200, persisted to
`.quickcode/settings.json`); a `confirm` float shows the server's own 409 reason
in `confirmRisk` and saves after confirming; a locked setting has no control at
all and its `PUT` still answers 403; the raw inspector opens over the view;
config → workspace round-trip keeps the transcript and the view state.

**Not verified:** the live WebSocket surviving the switch, because
`server/manager.py` was mid-rewrite during this pass and
`POST /api/conversations` answered 500 (`AttributeError:
'ConversationManager' object has no attribute '_frozen_composition'`). The
code-level guarantee holds — `disconnect()` is called from `showHome()` only —
but it wants one live click-through once the kernel work has landed.
