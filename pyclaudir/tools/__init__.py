"""Tool plugin package — drop a new file here, subclass BaseTool, and you're done.

The MCP server auto-discovers every ``BaseTool`` subclass found in modules in
this package at startup. No registry edits required.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from .base import BaseTool


def discover_tool_classes() -> list[type[BaseTool]]:
    """Walk ``pyclaudir.tools`` and return every concrete BaseTool subclass."""
    found: list[type[BaseTool]] = []
    seen: set[str] = set()
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name in {"base", "__init__"}:
            continue
        module = importlib.import_module(f"{__name__}.{mod_info.name}")
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
