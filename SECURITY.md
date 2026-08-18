# Security policy

## The shape of the thing

QuickCode is an agent that runs model-suggested tool calls — shell commands,
file reads, file edits — on your machine. That is the product, not a defect.
The safety boundary is the permission engine described in
`docs/PERMISSIONS.md`, not the model. Its server binds to the loopback
interface (127.0.0.1) only, and the local API is gated by a persistent
per-install token (`~/.quickcode/runtime.token`) plus Host/Origin
allowlisting.

`docs/COMPLIANCE.md` is the full disclosure document — every outbound
connection, every file written, the dependency licence table, the supply
chain, and an explicit list of the known gaps. If you are evaluating QuickCode
for an organisation, read that first; it names the weaknesses this policy
invites you to report.

## Reporting a vulnerability

Please do not report vulnerabilities in public issues.

**Email `kontakt@fichtelsystems.de`** with the affected version, reproduction
steps and expected impact. Expect an acknowledgement within a few working
days; QuickCode is maintained by one person, so please allow for that.

Email is the channel that works. There is also a
[GitHub private vulnerability reporting](https://github.com/devincii-io/QuickCode/security/advisories/new)
form, but private vulnerability reporting was **verified disabled** on this
repository on 2026-08-18, so that link will not open until the setting is
turned on. Try it if you prefer it; fall back to email, and do not treat a
non-opening form as a reason to file publicly.

Security fixes are provided for the latest published release only. Note that
`main` currently carries security fixes that no published release contains — see
the second list below.

## Reports that are particularly welcome

- A tool call that runs, edits, or reads outside the project root without a
  permission prompt.
- A way to reach `yolo`-mode behavior (unprompted execution) without the
  user having selected it.
- A stored API key or secret recoverable from disk, logs, or the session
  event log in plaintext.
- Any path by which a remote page or process (not the local user) can reach
  the loopback API.
- A way for a project's own committed files — `.quickcode/settings.json`,
  `.quickcode/plugins/`, or anything else read from the project tree — to run
  a program without the trust prompt, or to survive a grant they were not
  covered by. Opening a repository to read it must not execute it.
- A subagent obtaining a tool, a permission mode, or a model its parent was
  not granted. Delegation narrows; it must never widen.
- A bypass of the `web_fetch` SSRF guard: a URL that reaches a loopback,
  link-local, private or cloud-metadata address, or a redirect chain that is
  not re-validated hop by hop.

## What is already known, and does not need reporting

These are documented in `docs/COMPLIANCE.md` and are open, not secret. They are
listed as they stand on `main`; the released 2.0.0 additionally carries
everything under "fixed on `main`" below.

**Open:**

- The `yolo` circuit-breaker patterns are narrower than `docs/PERMISSIONS.md`
  describes, and subagents are constructed without the parent's rule set.
- A plugin file whose frontmatter declares `kind:` twice is classified by its
  first declaration for the trust hash and by its last one when it is loaded,
  so a previously trusted repository can add a command tool without
  re-prompting.
- `git` is invoked inside a project before the trust prompt, so a repository
  delivered with its `.git` directory intact can reach `core.fsmonitor`,
  `diff.external` or a `textconv` filter.
- The protected-path check returns before deny rules, so a `deny` against a
  protected path is downgraded to a prompt; `cd` out of the project root is not
  followed by later path checks; and "always allow" on a compound command
  persists a broader rule than was approved.
- Session transcripts are written **unredacted** to
  `<project>/.quickcode/sessions/`, with no retention or size limit. Nothing in
  the product redacts a secret pasted into chat, printed by a command, or living
  in an `AGENTS.md`. They are git-ignored now, which is not the same as safe.
- Stored API keys are protected by Windows DPAPI (user-bound), which does not
  defend against anything already running as that user — including QuickCode's
  own shell tool. The `bash` tool, the PTY and MCP subprocesses all inherit the
  full environment.
- The released Windows installer is not code-signed.
- The local server sets no `Content-Security-Policy`, and the loopback token is
  persistent per install and printed to stdout in the launch URL.

**Fixed on `main`, not yet in a published release** — still listed because the
2.0.0 you can download has them, so a report against 2.0.0 is not news:

- A leading environment assignment defeated the read-only bash auto-allow, so a
  read-only-looking command could execute in any mode including `plan`.
- `grep` and `glob` did not declare a path target, so they were not subject to
  the protected-path check and read outside the project root unprompted.
- Project-supplied `permissions.allow` rules and the
  `runtime.permissions.default_mode` setting in a committed
  `.quickcode/settings.json` were honoured without passing through the trust
  gate that covers `mcpServers` and command tools.
- QuickCode did not arrange for `.quickcode/` to be ignored by git, so a routine
  `git add -A` committed the transcripts.
- `scripts/bootstrap.ps1` provisioned Git and Python by downloading vendor
  installers without verifying a hash or signature. It now Authenticode-verifies
  both before executing them.

A concrete, working exploit of anything in the **open** list is still
interesting — a report that turns "plausible from reading the code" into "here
is the repro" is genuinely useful. A restatement of the bullet is not. A bypass
of one of the fixes, on `main`, is very interesting indeed.
