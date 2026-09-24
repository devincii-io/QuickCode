"""The agent workbench: what an agent gets, and why it does not get the rest.

``inventory``    every agent, ``@orchestrator`` first
``view``         one agent's whole answer, live or frozen (``/resolved``)
``drafts``       the same answer with an unsaved edit on top (``/preview``)
``resolution``   what an agent is resolved against, and under which parent
``prompt_view``  the composed prompt, block by block, with its absences
``tool_rows``    the tool picker's rows and groups, the schemas, the footer
``provenance``   reading a resolution's chain the way the view shows it

The routes live in ``server/agents_api.py``.
"""
