"""End-to-end MCP integration smoke tests.

These tests spawn the real ``claude`` CLI against a real third-party
MCP server (DeepWiki — public, no auth) to prove the full pipeline
works: ``plugins.json`` → ``McpPluginSpec`` → ``--mcp-config`` JSON
shape → Claude Code accepts and connects → MCP server responds.

Run as part of the regular suite. Requirements:

* the ``claude`` CLI on ``$PATH`` (a hard prerequisite for pyclaudir
  itself, so any dev environment that runs this repo's tests already
  has it). If genuinely missing, the test skips with a clear message
  rather than failing.
* outbound HTTPS to ``mcp.deepwiki.com``
* ~15 seconds total

If DeepWiki's backend 500s mid-test, the second test marks itself
``xfail`` rather than failing the suite — the goal is to validate
pyclaudir's plumbing, not their uptime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pyclaudir.plugins import load_plugins

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI not on PATH — install Claude Code to run integration tests",
)


def _build_mcp_config(plugin_name: str, plugins_path: Path) -> Path:
    """Replicate ``__main__.py``'s per-transport config-dict assembly
    for a single named plugin and write the resulting ``mcp.json``."""
    plugins = load_plugins(plugins_path, env={})
    target = next((m for m in plugins.mcps if m.name == plugin_name), None)
    if target is None:
        pytest.fail(f"plugin {plugin_name!r} not found in {plugins_path}")

    if target.type == "stdio":
        entry: dict = {
            "type": "stdio",
            "command": target.command,
            "args": list(target.args),
            "env": dict(target.env),
        }
    else:
        entry = {"type": target.type, "url": target.url}
        if target.headers:
            entry["headers"] = dict(target.headers)

    cfg = {"mcpServers": {target.name: entry}}
    out = Path(tempfile.mktemp(suffix=".json", prefix="pyclaudir-it-"))
    out.write_text(json.dumps(cfg, indent=2))
    return out


def _run_claude(mcp_config: Path, allowed: str, prompt: str, *, timeout: int = 60) -> str:
    """Invoke ``claude --print`` headlessly with the given MCP config.

    Returns stdout. Pins the test to Sonnet at low effort for speed
    and cost; pyclaudir itself can still run Opus in production.
    """
    proc = subprocess.run(
        [
            "claude", "--print",
            "--mcp-config", str(mcp_config),
            "--strict-mcp-config",
            "--allowedTools", allowed,
            "--model", "claude-sonnet-4-6",
            "--effort", "low",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        pytest.fail(f"claude exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc.stdout


@pytest.fixture(scope="module")
def deepwiki_config(tmp_path_factory) -> Path:
    """Build a one-entry mcp.json for the DeepWiki HTTP MCP.

    Doesn't depend on the operator's local ``plugins.json`` — synthesises
    the entry inline so the test is portable and idempotent.
    """
    spec_path = tmp_path_factory.mktemp("plugins") / "plugins.json"
    spec_path.write_text(json.dumps({
        "mcps": [
            {
                "name": "deepwiki",
                "type": "http",
                "url": "https://mcp.deepwiki.com/mcp",
                "allowed_tools": ["mcp__deepwiki"],
                "enabled": True,
            }
        ]
    }))
    return _build_mcp_config("deepwiki", spec_path)


def test_http_transport_lists_deepwiki_tools(deepwiki_config: Path) -> None:
    """Claude Code accepts the HTTP-transport config pyclaudir produces,
    connects to DeepWiki, and the three known tools are reachable.

    This validates the entire wire format end-to-end. If DeepWiki ever
    renames or drops a tool we'll see it as a clean failure here, not
    a mysterious bug in production.
    """
    output = _run_claude(
        deepwiki_config,
        "mcp__deepwiki",
        "List every tool you got from the deepwiki MCP server. Reply "
        "with just the bullet list of mcp__deepwiki__* names — no "
        "commentary.",
    )
    for tool in (
        "mcp__deepwiki__ask_question",
        "mcp__deepwiki__read_wiki_contents",
        "mcp__deepwiki__read_wiki_structure",
    ):
        assert tool in output, f"{tool} missing from claude output:\n{output}"


def test_http_transport_real_tool_call(deepwiki_config: Path) -> None:
    """Round-trip a real tool call through the HTTP transport.

    ``read_wiki_structure`` is the lightest DeepWiki tool — it just
    returns page titles, no LLM-side processing on their end. If
    pyclaudir's config-dict shape is even slightly wrong, Claude
    Code will refuse to call the tool or the server will reject it.
    """
    output = _run_claude(
        deepwiki_config,
        "mcp__deepwiki",
        "Use mcp__deepwiki__read_wiki_structure on the "
        "modelcontextprotocol/python-sdk repo. Then tell me ONLY the "
        "first page title it returned, as one line. No other prose.",
        timeout=90,
    )
    if "500" in output and ("Internal Server Error" in output or "transient" in output.lower()):
        pytest.xfail("DeepWiki backend returned 500 — server-side issue, not pyclaudir")
    # The repo's wiki has known top-level pages like "Overview" or
    # "FastMCP Server Framework". We don't pin the exact string (their
    # wiki structure can drift); just assert we got *something* that
    # looks like real wiki content rather than an error or empty.
    assert output.strip(), "claude returned empty output"
    assert "error" not in output.lower() or "500" in output, (
        f"unexpected error output:\n{output}"
    )
