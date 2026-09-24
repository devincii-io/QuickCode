# Trust gate — frontend handoff (Phase 3/6)

The backend trust gate is live (fixes the silent MCP RCE, finding F of
`scripts/qc_attack.py`). A project's MCP servers — the executable-bearing
project-scope config in `.quickcode/settings.json` /
`.quickcode/settings.local.json` — are **inert until the project is explicitly
trusted once**. This is enforced server-side; opening an untrusted project can
no longer execute code.

What remains is the **UI**: the prompt that lets a human make and revoke that
trust decision. The backend exposes everything that prompt needs.

## Backend contract (already shipped)

Trust is recorded at user scope in `~/.quickcode/trust.json`, keyed by resolved
project path, bound to a SHA-256 hash of the project's `mcpServers`. Editing the
config (adding/changing a server) changes the hash and re-prompts. See
`quickcode/security/trust.py`.

### Endpoints

Both the default-project shape and the `{pid}` shape exist, mirroring every
other route pair.

| Method + path | Purpose |
| --- | --- |
| `GET /api/trust` · `GET /api/projects/{pid}/trust` | Report trust status for the project. |
| `POST /api/trust` · `POST /api/projects/{pid}/trust` | Grant trust and connect the now-permitted MCP servers live. |
| `DELETE /api/trust` · `DELETE /api/projects/{pid}/trust` | Revoke trust. |

All require the loopback token, same as every `/api/*` route.

### `GET .../trust` response

```json
{
  "trusted": false,
  "has_servers": true,
  "servers": ["docs", "evil"],
  "hash": "44136fa3...",
  "inert": true,
  "reason": "project not trusted; its MCP servers are inert until you approve them",
  "running": []
}
```

- `has_servers` — the project declares project-scope MCP servers at all.
- `servers` — their names (what the prompt should list for review).
- `inert` — **true means servers were declared but refused for want of trust.**
  This is the flag that should raise the trust prompt / banner.
- `trusted` — the current config is trusted.
- `running` — servers actually started for this open project.
- `reason` — human-readable; distinguishes "never trusted" from "config changed
  since it was trusted".

### `POST .../trust` response

Same shape as `GET`, plus `"connected": ["docs", "evil"]` — the servers spawned
live by the grant. After a successful grant, `trusted` is `true` and `running`
includes the connected servers.

### `DELETE .../trust` response

Same shape as `GET`, plus `"revoked": true|false` (whether a grant existed)
and `"stopped": [...]` — the project's MCP servers the revocation stopped.

## What the UI must build

1. **Detect the refusal.** After opening a project, call `GET .../trust`. If
   `inert` is true, the project has MCP servers that were not started.

2. **A visible, non-silent banner / card.** The requirement: a project that
   quietly loses its MCP tools is a worse bug than the RCE. So the UI must say,
   plainly, "N MCP server(s) from this project were not started because the
   project is not trusted", list `servers` by name, and offer **Trust** and a
   way to inspect the config (the servers live in
   `.quickcode/settings.json` → `mcpServers`; consider showing the raw block so
   the user reviews the commands before trusting).

3. **Trust action.** `POST .../trust` with `{"hash": <the hash the prompt showed>}`;
   a `409` means the files changed after they were read — re-fetch and re-show. On success, surface `connected` ("started
   2 servers") and clear the banner. New conversations in the project pick up
   the tools immediately; a conversation already open keeps its toolset until
   restarted — tell the user that if a session is live.

4. **Revoke action** (Settings → project, or the same card). `DELETE .../trust`.
   Note in the copy: revocation takes effect now. The project's MCP servers
   are stopped and leave new chats' tool lists; a chat already open keeps the
   tool names, but calls to them fail, and its command tools refuse to run.

5. **Re-prompt on change.** If `reason` indicates the config changed since it
   was trusted (`trusted:false` while a prior grant existed), the banner should
   read "this project's MCP configuration changed — re-approve", not the
   first-time copy.

## Design judgement calls baked into the backend

- **The launch directory (`qc .`) is NOT implicitly trusted.** Cloning a repo
  and running `qc .` inside it is the exact attack; the user typing the path
  says nothing about whether the attacker-authored settings file inside is
  safe. It goes through the same gate as any opened project.
- **User-scope `~/.quickcode/settings.json` servers are never gated.** They are
  the user's own files; there is no attacker to defend against, and prompting
  for them trains the reflex that makes the project prompt worthless.
- **Revoke takes effect immediately.** "Revoke" is pressed by someone who has
  just decided they do not want this project's code running, so the ProjectHub
  stops the project's MCP processes (never the user's own), and a command tool
  checks trust on every call rather than only when a session opens.
