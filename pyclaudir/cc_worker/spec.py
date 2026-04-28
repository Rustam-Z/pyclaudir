"""Spawn-time configuration for the Claude Code subprocess.

Holds the dataclass that captures every CLI argument we will pass to
``claude``, the tool-allow/deny constants that gate dangerous built-ins,
and :func:`build_argv` — the single entry point that turns a
:class:`CcSpawnSpec` into the exact argv we hand to
``asyncio.create_subprocess_exec``.

Pinned by ``tests/test_security_invariants.py`` and
``tests/test_cc_worker_argv.py`` — every change here must keep those
tests green.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


#: Built-in tools we explicitly deny by default. Belt-and-braces with
#: ``--allowedTools`` so even if Claude Code's allowlist behaviour ever
#: changes, every dangerous tool is still off. Tools listed neither in
#: allow nor deny are implicitly reachable via ToolSearch — so this list
#: needs to cover *every* sensitive built-in, not just the ones we
#: previously cared about.
#:
#: ``WebFetch`` and ``WebSearch`` are *not* on this list — they're in
#: ``BASE_ALLOWED_TOOLS`` because the bot needs fresh info. The trade is:
#: data could be exfiltrated via URL or used to hit SSRF-able internal
#: addresses if asked nicely. The system prompt's internal-URL refusal
#: + Telegram-only output channel are the bounding mitigations.
#:
#: Each tool category here is unlocked by a corresponding ``enable_*``
#: field on :class:`CcSpawnSpec`, populated from ``plugins.json``'s
#: ``tool_groups`` block (``bash`` / ``code`` / ``subagents``). Default
#: off; ``plugins.json`` is the only source of truth — no env-var
#: overrides. See ``docs/tools.md``.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    # Shell execution — unlocked by ``enable_bash``.
    "Bash",
    "PowerShell",
    "Monitor",
    # Code work — unlocked by ``enable_code``.
    "Edit",
    "Write",
    "Read",
    "NotebookEdit",
    "Glob",
    "Grep",
    "LSP",
    # Subagents — unlocked by ``enable_subagents`` (existing flag).
    # ``Agent`` is added here at ``build_argv`` time when the flag is off.
)

#: Always-allowed tools, regardless of any ``enable_*`` flag. These are
#: the bot's core surface — the local pyclaudir MCP server (send_message,
#: memory, reminders, etc.) and read-only web tools.
BASE_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__pyclaudir",
    "WebFetch",
    "WebSearch",
)

#: Tools unlocked when ``enable_bash`` is True.
BASH_TOOLS: tuple[str, ...] = ("Bash", "PowerShell", "Monitor")

#: Tools unlocked when ``enable_code`` is True.
CODE_TOOLS: tuple[str, ...] = (
    "Edit", "Write", "Read", "NotebookEdit", "Glob", "Grep", "LSP",
)

#: Forbidden flag — never pass this. ``build_argv`` enforces it at build
#: time and the worker re-asserts it at spawn time.
FORBIDDEN_FLAG = "--dangerously-skip-permissions"


@dataclass(frozen=True)
class CcSpawnSpec:
    binary: str
    model: str
    system_prompt_path: Path
    mcp_config_path: Path
    json_schema_path: Path
    project_prompt_path: Path | None = None
    effort: str = "high"
    session_id: str | None = None
    #: If set, raw stdout/stderr from the CC subprocess is appended to
    #: ``<cc_logs_dir>/<session_id>.stream.jsonl`` and ``<session_id>.stderr.log``
    #: as the data arrives. Set to ``None`` to disable raw capture.
    cc_logs_dir: Path | None = None
    #: When True, the ``Agent`` tool is added to ``--allowedTools`` and the
    #: subagent documentation (``subagents_prompt_path``) is appended to the
    #: system prompt. When False (default), ``Agent`` is added to
    #: ``--disallowedTools`` and the docs file is not read — the bot cannot
    #: spawn subagents and doesn't even see the capability. Subagent turns
    #: are token-heavy; keep off unless you need them. Sourced from
    #: ``plugins.json`` ``tool_groups.subagents``.
    enable_subagents: bool = False
    #: Path to the subagent docs markdown. Read and appended to the system
    #: prompt iff ``enable_subagents`` is True. Ignored otherwise.
    subagents_prompt_path: Path | None = None
    #: When True, ``Bash``, ``PowerShell``, ``Monitor`` move from the deny
    #: list to the allow list. Sourced from ``plugins.json``
    #: ``tool_groups.bash``.
    enable_bash: bool = False
    #: When True, ``Edit``, ``Write``, ``Read``, ``NotebookEdit``, ``Glob``,
    #: ``Grep``, ``LSP`` move from deny to allow. Sourced from
    #: ``plugins.json`` ``tool_groups.code``.
    enable_code: bool = False
    #: Flat list of tool entries to add to ``--allowedTools`` from
    #: external MCP plugins. Each entry is either an exact tool name
    #: (``mcp__mcp-atlassian__jira_search``) or a server-prefix shorthand
    #: (``mcp__github``). Both forms are accepted by
    #: Claude Code in the same comma-separated allowlist. ``__main__``
    #: builds this from ``plugins.json`` after credential interpolation —
    #: an MCP whose ``${VAR}`` refs aren't satisfied contributes nothing
    #: here, preserving today's "credentials missing → tools hidden"
    #: semantics.
    mcp_allowed_tools: tuple[str, ...] = ()


def build_argv(spec: CcSpawnSpec) -> list[str]:
    """Construct the exact argv we hand to ``asyncio.create_subprocess_exec``.

    Pinned by ``tests/test_security_invariants.py``.
    """
    if not spec.system_prompt_path.exists():
        raise FileNotFoundError(spec.system_prompt_path)
    if not spec.mcp_config_path.exists():
        raise FileNotFoundError(spec.mcp_config_path)
    if not spec.json_schema_path.exists():
        raise FileNotFoundError(spec.json_schema_path)

    runtime_block = (
        "# Runtime\n\n"
        "You are running with:\n"
        f"- model: `{spec.model}`\n"
        f"- effort: `{spec.effort}`\n\n"
        "If a user asks which model or effort level you are running on, "
        "answer honestly with these exact values. This is public info — "
        "the hard boundary against revealing internal config does not apply "
        "to these two fields.\n"
    )
    system_prompt = spec.system_prompt_path.read_text(encoding="utf-8")
    if spec.project_prompt_path and spec.project_prompt_path.exists():
        system_prompt += "\n\n" + spec.project_prompt_path.read_text(encoding="utf-8")
    system_prompt += "\n\n" + runtime_block
    if spec.enable_subagents:
        if spec.subagents_prompt_path is None or not spec.subagents_prompt_path.exists():
            raise FileNotFoundError(
                "enable_subagents=True but subagents_prompt_path is missing: "
                f"{spec.subagents_prompt_path!r}"
            )
        system_prompt += "\n\n" + spec.subagents_prompt_path.read_text(encoding="utf-8")
    json_schema = spec.json_schema_path.read_text(encoding="utf-8")
    json.loads(json_schema)  # sanity check

    # Assemble allow/deny lists from the base sets plus whatever the
    # ``enable_*`` flags unlock. Tools listed in *neither* allow nor deny
    # are implicitly reachable via ToolSearch — so every gated tool must
    # land in one or the other.
    allowed_extras: list[str] = []
    disallowed_extras: list[str] = list(DEFAULT_DISALLOWED_TOOLS)

    def _unlock(tools: tuple[str, ...]) -> None:
        for t in tools:
            if t in disallowed_extras:
                disallowed_extras.remove(t)
            allowed_extras.append(t)

    if spec.enable_bash:
        _unlock(BASH_TOOLS)
    if spec.enable_code:
        _unlock(CODE_TOOLS)
    if spec.enable_subagents:
        allowed_extras.append("Agent")
    else:
        disallowed_extras.append("Agent")
    allowed_extras.extend(spec.mcp_allowed_tools)

    allowed_tools = BASE_ALLOWED_TOOLS + tuple(allowed_extras)
    disallowed_tools = tuple(disallowed_extras)

    argv: list[str] = [
        spec.binary,
        "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--model", spec.model,
        "--effort", spec.effort,
        "--system-prompt", system_prompt,
        "--mcp-config", str(spec.mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools", ",".join(allowed_tools),
        "--disallowedTools", ",".join(disallowed_tools),
        "--json-schema", json_schema,
    ]
    if spec.session_id:
        argv += ["--resume", spec.session_id]

    if FORBIDDEN_FLAG in argv:
        raise RuntimeError(
            f"refusing to build argv containing {FORBIDDEN_FLAG!r}; this flag "
            "is forbidden in pyclaudir under all circumstances"
        )
    return argv
