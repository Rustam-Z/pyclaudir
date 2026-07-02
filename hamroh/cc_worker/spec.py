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
#: the bot's core surface — the local hamroh MCP server (telegram_send_message,
#: memory, reminders, etc.) and read-only web tools.
BASE_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__hamroh",
    "WebFetch",
    "WebSearch",
)

#: The hamroh MCP namespace prefix. Claude Code registers every hamroh tool
#: as ``mcp__hamroh__<name>``; that prefixed form is the exact callable string.
#: Mirrors ``_MCP_PREFIX`` in ``event_handlers.py`` and ``tools/tools.py``.
_MCP_PREFIX = "mcp__hamroh__"

#: Built-in CC tools that are always reachable, regardless of any ``enable_*``
#: flag. ``StructuredOutput`` is the turn-end tool the worker keys on (see
#: ``event_handlers.py``); ``WebFetch``/``WebSearch`` give the bot fresh info;
#: the MCP-discovery/resource tools (``ToolSearch`` finds deferred tools,
#: ``List/ReadMcpResourceTool`` reach MCP *resources*, ``WaitForMcpServers``
#: blocks on a still-connecting server) let the bot reach external MCP servers
#: — harmless when none are configured (nothing to find), read-only otherwise.
#: These seed ``--tools`` — an *exclusive* allowlist over the built-in set — so
#: anything omitted (native ``Skill``, stray built-ins) is unreachable by
#: construction, not merely un-auto-approved. See :func:`_builtin_tools`.
#:
#: Full-catalog audit (code.claude.com/docs/en/tools-reference) — everything
#: else stays OFF on purpose; do NOT re-add a duplicate or dead-end tool:
#:   - ``Skill``                     → hamroh uses ``mcp__hamroh__skill_read``
#:   - ``Cron{Create,Delete,List}``  → hamroh has ``mcp__hamroh__reminder_*``
#:   - ``PushNotification``/``SendUserFile`` → hamroh delivers over Telegram
#:   - ``AskUserQuestion``           → interactive UI, dead in ``--print`` mode
#:   - ``Artifact``/``RemoteTrigger``/``ScheduleWakeup``/``ShareOnboardingGuide``
#:                                    → claude.ai / Team-plan / Remote-Control
#:   - ``Enter/ExitPlanMode``, ``Enter/ExitWorktree``, ``Workflow``,
#:     ``TodoWrite``, ``ReportFindings`` → dev-loop / deprecated / N/A here.
BASE_BUILTIN_TOOLS: tuple[str, ...] = (
    "WebFetch",
    "WebSearch",
    "StructuredOutput",
    "ToolSearch",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "WaitForMcpServers",
)

#: Session task-checklist tools (no permission) — let the bot track multi-step
#: turns (e.g. a research digest fanning out over many sources). Always on.
#: ``TaskStop``/``TaskOutput`` are omitted: background-task control the
#: fire-and-forget bot doesn't need (``TaskOutput`` is deprecated upstream).
TASK_TOOLS: tuple[str, ...] = ("TaskCreate", "TaskGet", "TaskList", "TaskUpdate")

#: Tools unlocked when ``enable_subagents`` is True. ``Agent`` spawns the
#: subagent; ``SendMessage`` lets the parent resume/steer a background one.
SUBAGENT_TOOLS: tuple[str, ...] = ("Agent", "SendMessage")

#: Tools unlocked when ``enable_bash`` is True.
BASH_TOOLS: tuple[str, ...] = ("Bash", "PowerShell", "Monitor")

#: Tools unlocked when ``enable_code`` is True.
CODE_TOOLS: tuple[str, ...] = (
    "Edit",
    "Write",
    "Read",
    "NotebookEdit",
    "Glob",
    "Grep",
    "LSP",
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
    #: Pre-rendered "available skills" block (name + description per skill),
    #: appended to the system prompt so the agent always knows what
    #: playbooks exist without calling ``skill_list`` — Agent Skills
    #: "level 1" metadata. Built at startup from
    #: :func:`hamroh.skills_store.render_skills_index`; empty string means
    #: nothing is appended (no skills, or feature off).
    skills_index: str = ""
    #: Exact names of the enabled hamroh MCP tools (bare, without the
    #: ``mcp__hamroh__`` prefix), used to render the "# Your tools" inventory
    #: baked into the system prompt. Populated at startup from the live tool
    #: instances; empty means the inventory block is skipped.
    hamroh_tool_names: tuple[str, ...] = ()


def _builtin_tools(spec: CcSpawnSpec) -> tuple[str, ...]:
    """The exclusive built-in allowlist handed to ``--tools``.

    Always-on base (web + turn-end + MCP-discovery) + task tools, plus
    whatever the ``enable_*`` flags unlock. Single source of truth reused by
    the ``--tools`` argv flag and the prompt inventory
    (:func:`render_tools_index`) so the two can never disagree."""
    tools: list[str] = list(BASE_BUILTIN_TOOLS) + list(TASK_TOOLS)
    if spec.enable_bash:
        tools.extend(BASH_TOOLS)
    if spec.enable_code:
        tools.extend(CODE_TOOLS)
    if spec.enable_subagents:
        tools.extend(SUBAGENT_TOOLS)
    return tuple(tools)


def _external_server_prefixes(entries: tuple[str, ...]) -> tuple[str, ...]:
    """Distinct ``mcp__<server>`` prefixes from external MCP allow entries.

    Each entry is either a server-prefix shorthand (``mcp__github``) or an
    exact tool name (``mcp__github__search``); both reduce to the same
    ``mcp__<server>`` prefix for the inventory. hamroh's own namespace is
    excluded — it has its own dedicated section."""
    prefixes: list[str] = []
    for entry in entries:
        parts = entry.split("__")
        if len(parts) < 2 or parts[0] != "mcp" or parts[1] == "hamroh":
            continue
        prefix = f"mcp__{parts[1]}"
        if prefix not in prefixes:
            prefixes.append(prefix)
    return tuple(prefixes)


def render_tools_index(spec: CcSpawnSpec) -> str:
    """Render the authoritative "# Your tools" block for the system prompt.

    Lists every reachable tool by its EXACT callable name across the three
    namespaces (hamroh-prefixed, bare built-in, external-prefixed) so the
    model copies names instead of reconstructing them — the root fix for
    wrong-name tool calls. Built-ins come from :func:`_builtin_tools`, the
    same source as ``--tools``, so the prompt never advertises a tool the
    model cannot call. Returns "" when there are no hamroh tools to anchor."""
    if not spec.hamroh_tool_names:
        return ""
    hamroh = "\n".join(f"- `{_MCP_PREFIX}{n}`" for n in sorted(spec.hamroh_tool_names))
    builtins = "\n".join(f"- `{n}`" for n in _builtin_tools(spec))
    block = (
        "# Your tools\n\n"
        "Call every tool by its EXACT name below — copy it, never rebuild it "
        "from memory or from the short names used elsewhere in this prompt. "
        "The `mcp__` prefix belongs ONLY to MCP tools; built-ins are bare.\n\n"
        "## hamroh tools — call with the `mcp__hamroh__` prefix\n"
        f"{hamroh}\n\n"
        "## Built-in tools — bare name, NEVER prefix\n"
        "(e.g. `WebFetch`, never `mcp__hamroh__WebFetch`)\n"
        f"{builtins}\n"
    )
    external = _external_server_prefixes(spec.mcp_allowed_tools)
    if external:
        ext = "\n".join(f"- `{p}__<tool>`" for p in external)
        block += (
            "\n## External MCP tools — call with the `mcp__<server>__` prefix\n"
            "Discover exact names via ToolSearch, then call them verbatim:\n"
            f"{ext}\n"
        )
    return block


def _compose_system_prompt(spec: CcSpawnSpec) -> str:
    """Assemble the system prompt: shipped base + project overlay +
    runtime block + (optionally) the skills index + (optionally) the tools
    inventory + (optionally) the subagent docs."""
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
    if spec.skills_index:
        system_prompt += "\n\n" + spec.skills_index
    tools_index = render_tools_index(spec)
    if tools_index:
        system_prompt += "\n\n" + tools_index
    if spec.enable_subagents:
        if (
            spec.subagents_prompt_path is None
            or not spec.subagents_prompt_path.exists()
        ):
            raise FileNotFoundError(
                "enable_subagents=True but subagents_prompt_path is missing: "
                f"{spec.subagents_prompt_path!r}"
            )
        system_prompt += "\n\n" + spec.subagents_prompt_path.read_text(encoding="utf-8")
    return system_prompt


def _tool_lists(spec: CcSpawnSpec) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Assemble ``(allowed, disallowed)`` from the base sets plus whatever
    the ``enable_*`` flags unlock. Tools listed in *neither* list are
    implicitly reachable via ToolSearch — so every gated tool must land
    in one or the other."""
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

    return BASE_ALLOWED_TOOLS + tuple(allowed_extras), tuple(disallowed_extras)


@dataclass(frozen=True)
class _ArgvParts:
    """Pre-computed pieces of the argv, assembled once and handed to
    :func:`_assemble_argv`."""

    system_prompt: str
    json_schema: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]


def _require_input_files(spec: CcSpawnSpec) -> None:
    """Raise :class:`FileNotFoundError` if any required input file is missing."""
    if not spec.system_prompt_path.exists():
        raise FileNotFoundError(spec.system_prompt_path)
    if not spec.mcp_config_path.exists():
        raise FileNotFoundError(spec.mcp_config_path)
    if not spec.json_schema_path.exists():
        raise FileNotFoundError(spec.json_schema_path)


def _assemble_argv(spec: CcSpawnSpec, parts: _ArgvParts) -> list[str]:
    """Build the flat argv list from the pre-computed prompt, schema, and
    tool lists. ``--resume`` is appended only when a session id is set."""
    argv: list[str] = [
        spec.binary,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        spec.model,
        "--effort",
        spec.effort,
        "--system-prompt",
        parts.system_prompt,
        "--mcp-config",
        str(spec.mcp_config_path),
        "--strict-mcp-config",
        "--tools",
        ",".join(_builtin_tools(spec)),
        "--allowedTools",
        ",".join(parts.allowed_tools),
        "--disallowedTools",
        ",".join(parts.disallowed_tools),
        "--json-schema",
        parts.json_schema,
    ]
    if spec.session_id:
        argv += ["--resume", spec.session_id]
    return argv


def build_argv(spec: CcSpawnSpec) -> list[str]:
    """Construct the exact argv we hand to ``asyncio.create_subprocess_exec``.

    Pinned by ``tests/test_security_invariants.py``.
    """
    _require_input_files(spec)

    json_schema = spec.json_schema_path.read_text(encoding="utf-8")
    json.loads(json_schema)  # sanity check
    allowed_tools, disallowed_tools = _tool_lists(spec)

    parts = _ArgvParts(
        system_prompt=_compose_system_prompt(spec),
        json_schema=json_schema,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )
    argv = _assemble_argv(spec, parts)

    if FORBIDDEN_FLAG in argv:
        raise RuntimeError(
            f"refusing to build argv containing {FORBIDDEN_FLAG!r}; this flag "
            "is forbidden in hamroh under all circumstances"
        )
    return argv
