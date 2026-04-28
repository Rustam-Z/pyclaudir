"""The eight security invariants from the build prompt.

These tests are intentionally low-cunning regression guards. They become
load-bearing as the codebase grows, so do not delete them when refactoring.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import pyclaudir
from pyclaudir.cc_worker import (
    BASH_TOOLS,
    CODE_TOOLS,
    FORBIDDEN_FLAG,
    CcSpawnSpec,
    build_argv,
)
from pyclaudir.mcp_server import MCP_SERVER_NAME, discover_tool_classes

# Sample tool names from the three integration MCPs we ship by default
# in ``plugins.json``. The test suite no longer imports the full
# allowlists from ``cc_worker`` — the source of truth moved to
# ``plugins.json``. We keep representative samples here so the
# "default-locked-down" invariant can still assert that integration
# tools don't leak into the empty-spec allowlist.
SAMPLE_JIRA_TOOLS = (
    "mcp__mcp-atlassian__jira_search",
    "mcp__mcp-atlassian__jira_get_issue",
    "mcp__mcp-atlassian__jira_create_issue",
    "mcp__mcp-atlassian__jira_get_agile_boards",
)
SAMPLE_GITLAB_TOOLS = ("mcp__mcp-gitlab",)
SAMPLE_GITHUB_TOOLS = ("mcp__github",)

PKG_ROOT = Path(pyclaudir.__file__).parent
TOOLS_DIR = PKG_ROOT / "tools"


@pytest.fixture()
def fake_spec(tmp_path: Path) -> CcSpawnSpec:
    sp = tmp_path / "system.md"
    sp.write_text("Pretend system prompt.")
    subp = tmp_path / "subagents.md"
    subp.write_text("# Subagents\n\nPretend subagent docs with read-only rule.")
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"pyclaudir": {"type": "http", "url": "http://x/mcp"}}}))
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}))
    return CcSpawnSpec(
        binary="claude",
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
        subagents_prompt_path=subp,
    )


# ---------------------------------------------------------------------------
# Invariant 1: locked-down argv
#
# Defaults are conservative: only the base pyclaudir MCP surface plus
# WebFetch/WebSearch is allowed. Every dangerous built-in (Bash and friends,
# code-edit tools, navigation, subagents) is hard-denied unless an operator
# flips the corresponding ``enable_*`` flag. Tools listed in *neither* allow
# nor deny are implicitly reachable via ToolSearch — so the deny side has to
# carry every gated tool, not just the historically-cared-about ones.
#
# The flags come from ``plugins.json`` ``tool_groups`` (default off):
#   tool_groups.bash      → Bash, PowerShell, Monitor
#   tool_groups.code      → Edit, Write, Read, NotebookEdit, Glob, Grep, LSP
#   tool_groups.subagents → Agent
#   (Jira / GitLab / GitHub tools come in via ``mcp_allowed_tools``,
#    populated from ``plugins.json`` after credential interpolation.)
# ---------------------------------------------------------------------------


def _split_argv(argv: list[str]) -> tuple[str, str, str]:
    """Return (allowedTools value, disallowedTools value, system-prompt value)."""
    allowed_value = argv[argv.index("--allowedTools") + 1]
    deny_value = argv[argv.index("--disallowedTools") + 1]
    sp_value = argv[argv.index("--system-prompt") + 1]
    return allowed_value, deny_value, sp_value


def test_invariant_1_argv_default_locks_down_dangerous_tools(fake_spec: CcSpawnSpec) -> None:
    """Default fake_spec has every ``enable_*`` flag off — argv must show
    a tight allow list (base only) and deny *every* gated tool."""
    argv = build_argv(fake_spec)
    allowed_value, deny_value, _sp = _split_argv(argv)

    # Base allowlist is present.
    assert "mcp__pyclaudir" in allowed_value
    assert "WebFetch" in allowed_value
    assert "WebSearch" in allowed_value

    # No integration tools by default. ``fake_spec`` ships with
    # ``mcp_allowed_tools=()`` so none of the integration namespaces
    # should appear anywhere in the allowlist.
    for jira in SAMPLE_JIRA_TOOLS:
        assert jira not in allowed_value, f"{jira} leaked into default allowlist"
    assert "mcp__mcp-atlassian" not in allowed_value
    for gitlab in SAMPLE_GITLAB_TOOLS:
        assert gitlab not in allowed_value, f"{gitlab} leaked into default allowlist"
    for github in SAMPLE_GITHUB_TOOLS:
        assert github not in allowed_value, f"{github} leaked into default allowlist"
    # No Confluence / JSM / Bitbucket / ProForma ever.
    for blocked in ("confluence", "Confluence", "Compass", "bitbucket",
                    "service_desk", "proforma"):
        assert blocked not in allowed_value, f"{blocked} in allowedTools"

    # Every gated tool denied by default.
    for forbidden in ("Bash", "PowerShell", "Monitor",
                      "Edit", "Write", "Read", "NotebookEdit",
                      "Glob", "Grep", "LSP", "Agent"):
        assert forbidden in deny_value, f"{forbidden} missing from --disallowedTools"
        assert forbidden not in allowed_value, f"{forbidden} in --allowedTools default"

    # Web tools are never denied.
    assert "WebFetch" not in deny_value
    assert "WebSearch" not in deny_value

    # Effort flag must be present.
    assert "--effort" in argv


def test_invariant_1_argv_never_has_dangerously_skip(fake_spec: CcSpawnSpec) -> None:
    argv = build_argv(fake_spec)
    assert FORBIDDEN_FLAG not in argv
    assert FORBIDDEN_FLAG not in " ".join(argv)


def test_invariant_1_argv_subagents_enabled(fake_spec: CcSpawnSpec) -> None:
    """With enable_subagents=True, Agent moves from deny to allow and the
    subagent documentation block is appended to the system prompt."""
    import dataclasses
    spec_on = dataclasses.replace(fake_spec, enable_subagents=True)
    argv = build_argv(spec_on)
    allowed_value, deny_value, system_prompt = _split_argv(argv)

    assert "Agent" in allowed_value
    assert "Agent" not in deny_value
    # The other gated categories stay denied.
    for forbidden in ("Bash", "Edit", "Write", "Read", "NotebookEdit",
                      "PowerShell", "Monitor", "Glob", "Grep", "LSP"):
        assert forbidden in deny_value
    # Subagent docs appended.
    assert "# Subagents" in system_prompt
    assert "read-only" in system_prompt.lower()


def test_invariant_1_argv_bash_enabled(fake_spec: CcSpawnSpec) -> None:
    """enable_bash unlocks Bash, PowerShell, Monitor — and only those."""
    import dataclasses
    spec_on = dataclasses.replace(fake_spec, enable_bash=True)
    argv = build_argv(spec_on)
    allowed_value, deny_value, _sp = _split_argv(argv)

    for t in BASH_TOOLS:
        assert t in allowed_value, f"{t} should be allowed with enable_bash=True"
        assert t not in deny_value
    # Code tools and Agent stay denied.
    for forbidden in (*CODE_TOOLS, "Agent"):
        assert forbidden in deny_value, f"{forbidden} should still be denied"


def test_invariant_1_argv_code_enabled(fake_spec: CcSpawnSpec) -> None:
    """enable_code unlocks Edit/Write/Read/NotebookEdit/Glob/Grep/LSP."""
    import dataclasses
    spec_on = dataclasses.replace(fake_spec, enable_code=True)
    argv = build_argv(spec_on)
    allowed_value, deny_value, _sp = _split_argv(argv)

    for t in CODE_TOOLS:
        assert t in allowed_value, f"{t} should be allowed with enable_code=True"
        assert t not in deny_value
    for forbidden in (*BASH_TOOLS, "Agent"):
        assert forbidden in deny_value


def test_invariant_1_argv_jira_enabled(fake_spec: CcSpawnSpec) -> None:
    """When ``plugins.json`` advertises Jira tools (40 entries from
    mcp-atlassian), they all land in ``--allowedTools``. Source of
    truth for the full list is now ``plugins.json``; the test asserts
    every tool the spec hands to ``build_argv`` survives to the argv."""
    import dataclasses
    spec_on = dataclasses.replace(fake_spec, mcp_allowed_tools=SAMPLE_JIRA_TOOLS)
    argv = build_argv(spec_on)
    allowed_value, _deny, _sp = _split_argv(argv)

    for t in SAMPLE_JIRA_TOOLS:
        assert t in allowed_value, f"{t} missing from allowedTools"
    # No GitLab leak — only what was passed in goes through.
    assert "mcp__mcp-gitlab" not in allowed_value
    assert "mcp__github" not in allowed_value


def test_invariant_1_argv_gitlab_enabled(fake_spec: CcSpawnSpec) -> None:
    """The ``mcp__mcp-gitlab`` prefix in ``mcp_allowed_tools`` lands in
    ``--allowedTools`` and no other integration namespace leaks."""
    import dataclasses
    spec_on = dataclasses.replace(fake_spec, mcp_allowed_tools=SAMPLE_GITLAB_TOOLS)
    argv = build_argv(spec_on)
    allowed_value, _deny, _sp = _split_argv(argv)

    assert "mcp__mcp-gitlab" in allowed_value
    for jira in SAMPLE_JIRA_TOOLS:
        assert jira not in allowed_value
    assert "mcp__mcp-atlassian" not in allowed_value
    assert "mcp__github" not in allowed_value


def test_invariant_1_argv_github_enabled(fake_spec: CcSpawnSpec) -> None:
    """The ``mcp__github`` prefix in ``mcp_allowed_tools`` lands in
    ``--allowedTools`` and no other integration namespace leaks."""
    import dataclasses
    spec_on = dataclasses.replace(fake_spec, mcp_allowed_tools=SAMPLE_GITHUB_TOOLS)
    argv = build_argv(spec_on)
    allowed_value, _deny, _sp = _split_argv(argv)

    assert "mcp__github" in allowed_value
    for jira in SAMPLE_JIRA_TOOLS:
        assert jira not in allowed_value
    assert "mcp__mcp-atlassian" not in allowed_value
    assert "mcp__mcp-gitlab" not in allowed_value


# ---------------------------------------------------------------------------
# Invariant 2: MCP namespace lockdown
# ---------------------------------------------------------------------------


def test_invariant_2_only_pyclaudir_server_name() -> None:
    assert MCP_SERVER_NAME == "pyclaudir"
    # And every discovered tool's name is plain (no other prefixes baked in).
    for cls in discover_tool_classes():
        assert "__" not in cls.name, f"tool name {cls.name!r} sneaks a prefix"
        assert not cls.name.startswith("mcp_"), cls.name


# ---------------------------------------------------------------------------
# Invariant 3: memory writes exist but enforce safety rails
#
# Originally read-only. Operator opted the agent into write_memory + append_memory
# so it can keep its own notes. The trade-off is mitigated by:
#   (a) read-before-write — must read existing files before mutating them
#   (b) 64 KiB per-file size cap
#   (c) path-traversal hardening (already in place)
#   (d) NO delete tool (forgetting requires explicit overwrite)
#
# This test pins all four conditions so a future change can't quietly
# erode them.
# ---------------------------------------------------------------------------


def test_invariant_3_memory_write_safety_rails() -> None:
    """The expected memory tool surface is exactly: list/read/write/append.
    No delete/edit/create. No path traversal. Read-before-write enforced."""
    classes = {c.name: c for c in discover_tool_classes()}
    expected_memory_tools = {"list_memories", "read_memory", "write_memory", "append_memory"}
    actual_memory_tools = set(classes.keys()) & {
        "list_memories", "read_memory", "write_memory", "append_memory",
        "delete_memory", "edit_memory", "create_memory", "remove_memory",
        "rm_memory",
    }
    assert actual_memory_tools == expected_memory_tools, (
        f"unexpected memory tool surface: {actual_memory_tools}"
    )

    # No deletion tool exists in any form.
    forbidden_names = {"delete_memory", "edit_memory", "create_memory", "remove_memory", "rm_memory"}
    offending = [c.name for c in discover_tool_classes() if c.name in forbidden_names]
    assert offending == [], f"forbidden memory mutation tools registered: {offending}"


def test_invariant_3_read_before_write_enforced(tmp_path: Path) -> None:
    """The MemoryStore itself rejects overwrites of files it hasn't read."""
    from pyclaudir.memory_store import MemoryPathError, MemoryStore

    store = MemoryStore(tmp_path / "memories")
    store.ensure_root()
    (store.root / "operator_note.md").write_text("CRITICAL")
    with pytest.raises(MemoryPathError, match="read-before-write"):
        store.write("operator_note.md", "destroyed")
    # The original survived
    assert (store.root / "operator_note.md").read_text() == "CRITICAL"


def test_invariant_3_size_cap_enforced(tmp_path: Path) -> None:
    from pyclaudir.memory_store import MAX_MEMORY_BYTES, MemoryPathError, MemoryStore

    store = MemoryStore(tmp_path / "memories")
    store.ensure_root()
    with pytest.raises(MemoryPathError, match="too large"):
        store.write("huge.md", "x" * (MAX_MEMORY_BYTES + 1))


# ---------------------------------------------------------------------------
# Invariant 4: memory path containment (covered fully in test_memory_path_safety,
# we keep a smoke check here so this file alone proves the boundary exists).
# ---------------------------------------------------------------------------


def test_invariant_4_memory_path_traversal_rejected(tmp_path: Path) -> None:
    from pyclaudir.memory_store import MemoryPathError, MemoryStore

    store = MemoryStore(tmp_path / "memories")
    store.ensure_root()
    for hostile in (
        "../../../etc/passwd",
        "/etc/passwd",
        "data/memories/../../../etc/passwd",
        "notes/../../etc/passwd",
    ):
        with pytest.raises(MemoryPathError):
            store.resolve_path(hostile)


# ---------------------------------------------------------------------------
# Invariant 5: only memory.py reads files inside pyclaudir/tools/
# ---------------------------------------------------------------------------


_FILE_READ_FUNCS = {"open"}
_FILE_READ_METHODS = {"read_text", "read_bytes"}


def _file_read_offences(tree: ast.AST) -> list[str]:
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _FILE_READ_FUNCS:
                offences.append(f"open() at line {node.lineno}")
            if isinstance(f, ast.Attribute) and f.attr in _FILE_READ_METHODS:
                offences.append(f".{f.attr}() at line {node.lineno}")
    return offences


def test_invariant_5_no_file_reads_outside_memory_module() -> None:
    for path in TOOLS_DIR.rglob("*.py"):
        if path.name in {"__init__.py", "base.py", "memory.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offences = _file_read_offences(tree)
        assert offences == [], f"{path}: forbidden filesystem reads {offences}"


# ---------------------------------------------------------------------------
# Invariant 6: no shell execution from tools
# ---------------------------------------------------------------------------


_FORBIDDEN_SHELL_NAMES = {"system", "popen", "spawnl", "spawnv", "spawnvp", "execv", "execvp"}
_FORBIDDEN_SHELL_MODULES = {"subprocess", "os"}


def _shell_offences(tree: ast.AST) -> list[str]:
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                if f.attr in {"system", "popen"}:
                    offences.append(f"os.{f.attr}() line {node.lineno}")
                if f.attr.startswith("create_subprocess_") or f.attr in {"run", "Popen", "call", "check_call", "check_output"}:
                    val = f.value
                    if isinstance(val, ast.Name) and val.id in {"subprocess", "asyncio"}:
                        offences.append(f"{val.id}.{f.attr}() line {node.lineno}")
    return offences


def test_invariant_6_no_subprocess_in_tools() -> None:
    for path in TOOLS_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offences = _shell_offences(tree)
        assert offences == [], f"{path}: forbidden shell calls {offences}"
        # Also ban the imports themselves so no helper module can sneak it in.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in {"subprocess"}, f"{path} imports subprocess"
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"subprocess"}, f"{path} imports from subprocess"


# ---------------------------------------------------------------------------
# Invariant 7: owner-only privileged commands
# ---------------------------------------------------------------------------


def test_invariant_7_owner_check_via_gate() -> None:
    """The ``gate()`` function is the access decision point. The owner is
    always allowed; strangers are blocked under ``owner_only`` policy."""
    from pyclaudir.access import AccessConfig, gate

    access = AccessConfig(policy="owner_only", allowed_users=[], allowed_chats=[])
    assert gate(access=access, owner_id=42, chat_id=42, user_id=42, chat_type="private") is True
    assert gate(access=access, owner_id=42, chat_id=999, user_id=999, chat_type="private") is False
    assert gate(access=access, owner_id=42, chat_id=-100123, user_id=999, chat_type="supergroup") is False


# ---------------------------------------------------------------------------
# Invariant 8: query_db is SELECT-only (covered in detail when query_db lands
# in Step 11; we leave a placeholder here that auto-skips until then so the
# file's coverage stays honest).
# ---------------------------------------------------------------------------


def test_invariant_8_query_db_select_only_when_present() -> None:
    classes = {c.name: c for c in discover_tool_classes()}
    if "query_db" not in classes:
        pytest.skip("query_db not implemented yet (Step 11)")
    from pyclaudir.tools.query_db import is_safe_select  # type: ignore

    assert is_safe_select("SELECT 1") is True
    for hostile in (
        "SELECT 1; DROP TABLE messages;",
        "INSERT INTO messages(chat_id) VALUES (1)",
        "PRAGMA journal_mode",
        "ATTACH DATABASE '/tmp/x' AS x",
        "WITH bad AS (DELETE FROM messages RETURNING *) SELECT * FROM bad",
    ):
        assert is_safe_select(hostile) is False, f"is_safe_select accepted: {hostile!r}"
