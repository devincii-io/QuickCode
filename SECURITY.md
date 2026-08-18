# Security policy

QuickCode's server binds to the loopback interface (127.0.0.1) only. It runs
model-suggested tool calls — including shell commands and file edits —
against the permission engine described in `docs/PERMISSIONS.md`; that
engine, not the model, is the safety boundary. Please do not report
vulnerabilities in public issues.

Use [GitHub private vulnerability reporting](https://github.com/devincii-io/QuickCode/security/advisories/new)
and include the affected version, reproduction steps, and expected impact.
Particularly relevant reports:

- A tool call that runs, edits, or reads outside the project root without a
  permission prompt.
- A way to reach `yolo`-mode behavior (unprompted execution) without the
  user having selected it.
- A stored API key or secret recoverable from disk, logs, or the session
  event log in plaintext.
- Any path by which a remote page or process (not the local user) can reach
  the loopback API.

Security fixes are provided for the latest published release.
