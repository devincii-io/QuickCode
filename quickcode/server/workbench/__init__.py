"""The agent workbench: what an agent gets, and why it does not get the rest.

Three ideas hold this package together.

**One computation, never two.** ``/resolved`` and ``/preview`` call
``resolve_composition`` -- the same function ``manager.open()`` and
``spawn_subagent`` call -- and render the prompt through
``prompts/system.render_with_sections`` or ``prompts/subagent`` -- the same code
the runner renders with. Nothing here reconstructs a prompt or a tool list. A
reconstruction drifts, and a preview that drifts is worse than no preview,
because it is believed.

**Absences are answers.** A tool that is missing is listed with the reason it is
missing; a prompt section that did not render is listed with the reason it did
not. "You cannot see why you don't have it" is the failure this whole surface
exists to fix, so an omitted key is never the answer to "why".

**Live is labelled live.** A resolution against the current settings files says
``frozen: false``; a resolution read out of a running session's meta record says
``frozen: true`` and carries the digest it was recorded with. When the two
disagree the payload says so rather than picking one.

``inventory``     every agent, ``@orchestrator`` first
``view``          one agent's whole answer, live or frozen (``/resolved``)
``drafts``        the same answer with an unsaved edit on top (``/preview``)
``compositions``  saving an edit, duplicating to customise, switching a session
``resolution``    what an agent is resolved against, and under which parent
``prompt_view``   the composed prompt, block by block, with its absences
``tool_rows``     the tool picker's rows and groups, the schemas, the footer
``provenance``    reading a resolution's chain the way the view shows it

The routes live in ``server/agents_api.py``.
"""
