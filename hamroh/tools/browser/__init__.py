"""Browser-automation tools + the shared warm-Chromium machinery.

Each ``browser_*`` MCP tool is one module in this package; the manager,
session, SSRF guard, and ``BrowserSessionTool`` base they all share live in
:mod:`.browser`. The public session types are re-exported here so the rest of
hamroh keeps importing them as ``hamroh.tools.browser`` without reaching
into the submodule.
"""

from __future__ import annotations

from .browser import BrowserManager, BrowserSession, BrowserSessionTool

__all__ = ["BrowserManager", "BrowserSession", "BrowserSessionTool"]
