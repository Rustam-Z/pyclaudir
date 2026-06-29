"""Tool plugin package — drop a new file here, subclass BaseTool, and you're done.

The MCP server auto-discovers every ``BaseTool`` subclass found in modules in
this package at startup. No registry edits required. Discovery recurses into
subpackages (e.g. ``browser/`` and ``telegram/``), so tools can be grouped in
folders without any registration change.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from .base import BaseTool


def discover_tool_classes() -> list[type[BaseTool]]:
    """Walk ``hamroh.tools`` (recursively) and return every concrete
    BaseTool subclass. Subpackages like ``browser`` and ``telegram`` are
    descended into; the abstract ``base``/``BrowserSessionTool`` bases are
    skipped."""
    found: list[type[BaseTool]] = []
    seen: set[str] = set()
    for mod_info in pkgutil.walk_packages(__path__, prefix=f"{__name__}."):
        if mod_info.ispkg or mod_info.name.rsplit(".", 1)[-1] == "base":
            continue
        module = importlib.import_module(mod_info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseTool) or obj is BaseTool:
                continue
            if inspect.isabstract(obj):
                continue
            if obj.__name__ in seen:
                continue
            seen.add(obj.__name__)
            found.append(obj)
    return found
