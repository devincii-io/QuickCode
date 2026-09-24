"""Internal plugins: everything QuickCode ships, declared as plugin specs.

These are derived from the live objects wherever possible -- tool specs come
from the registry the agent will actually use, agent specs from the loaded
definitions, MCP specs from the configured servers. That is deliberate: a
manifest that restated the runtime from memory would drift, and the Settings
UI would start describing an app that no longer exists.

Tiers here are the contract from ``docs/archive/PLAN-PLUGIN-UI-OVERHAUL.md``. The
short version: how tools are called, how events are logged and how subagent
reports are sanitized are ``locked``; the knobs that move agent behaviour are
``confirm``; taste is ``free``.

**The prose is part of the manifest.** Every spec in this package answers the
six questions in ``kernel/spec.py`` -- what it is, what it affects, who it
reaches, what changes if you change it, and for the fixed ones why and what to
do instead. Two rules govern the writing:

* *Accuracy over fluency.* Every sentence here is checkable against the module
  it describes. A confident sentence that is wrong is worse than no sentence,
  so a field nobody could verify is left empty rather than filled.
* *Derived where derivable.* Tool and agent prose is computed from the tool's
  own ``PermissionSpec`` and the agent's ceiling and tool list, so a new tool
  gets a truthful card without anyone writing one. Only the facts that live in
  the wiring rather than in the object -- ``plan`` never reaching a subagent,
  the delegation pair being granted by depth -- are written by hand.

One module per family of plugin:

``core``       the runtime internals; their settings are declared in
               ``kernel/core_settings.py``, where the runtime reads them
``sections``   the system-prompt sections
``tools``      the live tools, and ``tool_group``
``agents``     subagent definitions, built-in and authored
``authored``   authored command tools and prompt sections
``providers``  model provider factories
``mcp``        configured MCP servers
"""

from quickcode.kernel.core_settings import core_setting
from quickcode.kernel.facts import display_endpoint, tool_signature
from quickcode.kernel.manifest.agents import SHIPPED_AGENTS, agent_specs, is_shipped_agent
from quickcode.kernel.manifest.authored import authored_specs
from quickcode.kernel.manifest.core import core_specs
from quickcode.kernel.manifest.mcp import REDACTED, mcp_specs
from quickcode.kernel.manifest.providers import provider_specs
from quickcode.kernel.manifest.sections import prompt_section_specs
from quickcode.kernel.manifest.tools import tool_group, tool_specs

__all__ = [
    "REDACTED",
    "SHIPPED_AGENTS",
    "agent_specs",
    "authored_specs",
    "core_setting",
    "core_specs",
    "display_endpoint",
    "is_shipped_agent",
    "mcp_specs",
    "prompt_section_specs",
    "provider_specs",
    "tool_group",
    "tool_signature",
    "tool_specs",
]
