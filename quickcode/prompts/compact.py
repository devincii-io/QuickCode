"""Compaction prompt: compress a long transcript into a continuation handoff.

Run as a one-off request (same model, no tools) when the token ledger crosses
the context threshold or on /compact. The model's output becomes the seed
message of the rebuilt history (see quickcode.core.compact).
"""

from __future__ import annotations

COMPACTION_PROMPT = """\
<task>
The conversation above is being compressed to continue in a fresh context.
Write a handoff summary for the agent that continues this work. It has no
memory beyond your summary and the last few verbatim turns. Optimize for
continuation, not narration.
</task>

<required_sections>
  <primary_request>
    The user's core request(s), with all explicit requirements. Quote the
    user verbatim where wording matters.
  </primary_request>
  <key_decisions>
    Decisions made and their rationale, including approaches that were
    tried and rejected (so they aren't retried).
  </key_decisions>
  <files>
    Every file read or modified: path, why it matters, and current state.
    Include exact code snippets only for sections mid-edit.
  </files>
  <errors_and_fixes>
    Errors hit, how they were fixed, anything still failing with the exact
    error text.
  </errors_and_fixes>
  <current_state>
    What is done and verified vs done-but-unverified vs not started.
  </current_state>
  <next_step>
    The immediate next action, precise enough to execute without re-deriving
    it. If the user gave instructions for later, quote them verbatim.
  </next_step>
</required_sections>

<rules>
- Facts only; no praise, no meta-commentary.
- Prefer paths, symbols, and commands over prose descriptions of them.
</rules>"""

POST_COMPACTION_REMINDER = (
    "Earlier conversation was summarized above. Trust the summary; re-read "
    "files before editing them."
)
