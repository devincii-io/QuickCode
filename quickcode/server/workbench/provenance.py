"""Reading a resolution's provenance chain the way the workbench shows it."""

from __future__ import annotations

from typing import Any

from quickcode.kernel.composition import Resolved


def prov_json(prov: Any) -> dict[str, Any]:
    return prov.to_json() if prov is not None else {}


def last_prov(resolved: Resolved, key: str) -> Any:
    """The layer that decided ``key``: the last entry of its chain, if any."""
    entries = resolved.chain.get(key) or ()
    return entries[-1] if entries else None
