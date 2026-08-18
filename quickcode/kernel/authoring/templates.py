"""What New... writes to disk before opening the editor.

A commented example teaches the format in one read; a form with eleven inputs
and no context is where an app loses people. These are real, loadable files:
every one of them validates as written, so the first thing a new author sees is
something that works rather than a stub that does not.

They are Python strings rather than data files on purpose -- the templates ship
inside the wheel with no packaging step to forget.
"""

from __future__ import annotations

TOOL = '''\
---
kind: tool
name: {name}
title: {title}
description: One or two sentences the model reads when deciding to call this.
group: Tools
# Shown in the transcript. Placeholders are substituted for display only.
label: {name} {{path}}
# project | file_dir | a path relative to the project root
cwd: project
timeout_ms: 120000
# text | json | lines | file
output: text
max_output_chars: 30000
success_exit_codes: [0]
# error: a non-zero exit is a failure. content: a non-zero exit is the answer
# (a linter, a test run).
on_nonzero: error
# Which parameter a permission rule like {name}(src/**) matches on.
permission_target: path
---

Everything outside the fenced blocks is the long description, appended to
`description` in the schema the model sees. Say what the command does, when to
reach for it, and what its exit codes mean.

```json params
[
  {{"name": "path", "type": "path", "required": false, "default": "",
   "description": "Optional file or directory to narrow the run to."}}
]
```

The command is an array, one element per argument, executed directly -- never
through a shell. A value containing `; rm -rf /` is one inert argument, because
nothing parses it.

Four rules worth knowing:

  - `{{path}}` inside an element substitutes in place: "--path={{path}}" stays
    one argument whatever the value contains.
  - an element that is exactly `{{path}}` and is empty is dropped, which is how
    an optional argument disappears. Give each optional flag its own element.
  - a list parameter must have an element to itself; it expands to one argument
    per item.
  - a bool parameter must have an element to itself; true emits `--<name>`,
    false drops it.

```json argv
["git", "status", "--short", "{{path}}"]
```
'''

AGENT = '''\
---
kind: agent
name: {name}
title: {title}
description: What this agent is for, and what it must not be given. The
  spawning agent reads this to decide what to delegate here.
group: Agents
# Names, aliases or globs, resolved against the live tool pool when the agent
# is spawned. Omit the key to inherit whatever the spawner holds.
tools: [read, glob, grep]
model: worker
models: [worker]
model_selectable: true
# The most this agent may ever do. It is intersected with the spawner's, so
# raising it here cannot lift the agent above the session it runs in.
mode_cap: ask
max_turns: 20
color: cyan
---

The body is the agent's system prompt, verbatim.

You are a subagent. Say what it reads, what it writes, and what its final
message must contain -- that message is the entire result the spawner sees;
nobody reads the intermediate steps.
'''

PROMPT = '''\
---
kind: prompt
name: {name}
title: {title}
description: What this section adds to the prompt, for the settings card.
group: Prompt
# Position in the composed prompt. The internal sections sit at 10..130.
# `after: prompt.conventions` is the same thing said by name.
after: prompt.conventions
applies_to: [main]
# always | plan | orchestration | headless
when: always
enabled_by_default: true
---

<{name}>
The body is the section text, verbatim. Wrapping it in a tag the way the
internal sections do is recommended and not enforced.
</{name}>
'''

_BY_KIND = {"tool": TOOL, "agent": AGENT, "prompt": PROMPT}


def template(kind: str, name: str, title: str = "") -> str:
    body = _BY_KIND.get(kind)
    if body is None:
        raise ValueError(f"no template for kind {kind!r}")
    return body.format(name=name, title=title or name.replace("-", " ").capitalize())
