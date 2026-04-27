"""Tests for the ``plugins.json`` loader.

Covers the schema validator, the ``${VAR}`` interpolator, the
"unresolved var → skip" semantics that preserve today's credential gate,
the "malformed file → crash boot" semantics that fail-fast on operator
typos, and the ``skills_disabled`` round-trip into ``SkillsStore``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyclaudir.plugins import (
    McpPluginSpec,
    Plugins,
    PluginsConfigError,
    load_plugins,
)
from pyclaudir.skills_store import SkillsStore


# ---------------------------------------------------------------------------
# Empty / missing / minimal
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    plugins = load_plugins(tmp_path / "absent.json")
    assert plugins == Plugins()
    assert plugins.mcps == ()
    assert plugins.skills_disabled == frozenset()
    assert plugins.tool_groups == {}


def test_empty_object_is_valid(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text("{}")
    plugins = load_plugins(p)
    assert plugins.mcps == ()


# ---------------------------------------------------------------------------
# Tool groups
# ---------------------------------------------------------------------------


def test_tool_groups_parsed(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"tool_groups": {"bash": True, "code": False}}))
    plugins = load_plugins(p)
    assert plugins.tool_groups == {"bash": True, "code": False}


def test_tool_groups_unknown_key_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"tool_groups": {"bashh": True}}))
    with pytest.raises(PluginsConfigError, match="bashh"):
        load_plugins(p)


def test_tool_groups_non_bool_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"tool_groups": {"bash": "yes"}}))
    with pytest.raises(PluginsConfigError, match="true/false"):
        load_plugins(p)


# ---------------------------------------------------------------------------
# Top-level schema
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"mcsp": []}))  # typo
    with pytest.raises(PluginsConfigError, match="mcsp"):
        load_plugins(p)


def test_invalid_json_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text("{not valid json")
    with pytest.raises(PluginsConfigError, match="invalid JSON"):
        load_plugins(p)


def test_top_level_array_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text("[]")
    with pytest.raises(PluginsConfigError, match="top level"):
        load_plugins(p)


# ---------------------------------------------------------------------------
# MCP entries
# ---------------------------------------------------------------------------


def _mcp(**overrides) -> dict:
    base = {
        "name": "demo",
        "command": "demo-cmd",
        "args": [],
        "env": {},
        "allowed_tools": ["mcp__demo"],
        "enabled": True,
    }
    base.update(overrides)
    return base


def test_mcp_minimal_loads(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"mcps": [_mcp()]}))
    plugins = load_plugins(p, env={})
    assert len(plugins.mcps) == 1
    assert plugins.mcps[0] == McpPluginSpec(
        name="demo",
        command="demo-cmd",
        args=(),
        env={},
        allowed_tools=("mcp__demo",),
    )


def test_mcp_missing_required_key_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    bad = _mcp()
    bad.pop("command")
    p.write_text(json.dumps({"mcps": [bad]}))
    with pytest.raises(PluginsConfigError, match="missing required"):
        load_plugins(p)


def test_mcp_unknown_key_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    bad = _mcp(transport="websocket")  # unknown
    p.write_text(json.dumps({"mcps": [bad]}))
    with pytest.raises(PluginsConfigError, match="transport"):
        load_plugins(p)


def test_mcp_empty_allowed_tools_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"mcps": [_mcp(allowed_tools=[])]}))
    with pytest.raises(PluginsConfigError, match="non-empty list"):
        load_plugins(p)


def test_mcp_duplicate_name_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"mcps": [_mcp(), _mcp()]}))
    with pytest.raises(PluginsConfigError, match="duplicate name"):
        load_plugins(p)


def test_mcp_disabled_is_skipped(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"mcps": [_mcp(enabled=False)]}))
    plugins = load_plugins(p, env={})
    assert plugins.mcps == ()


# ---------------------------------------------------------------------------
# ${VAR} interpolation
# ---------------------------------------------------------------------------


def test_var_substitution_in_env(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({
        "mcps": [_mcp(env={"TOKEN": "${MY_TOKEN}"})]
    }))
    plugins = load_plugins(p, env={"MY_TOKEN": "abc123"})
    assert plugins.mcps[0].env == {"TOKEN": "abc123"}


def test_var_substitution_concatenated(tmp_path: Path) -> None:
    """``${BASE}/api/v4`` must concatenate cleanly — matches the
    GitLab default's ``${GITLAB_URL}/api/v4`` pattern."""
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({
        "mcps": [_mcp(env={"URL": "${BASE}/api/v4"})]
    }))
    plugins = load_plugins(p, env={"BASE": "https://gitlab.example.com"})
    assert plugins.mcps[0].env == {"URL": "https://gitlab.example.com/api/v4"}


def test_unresolved_var_skips_mcp(tmp_path: Path) -> None:
    """An enabled MCP whose ``${VAR}`` resolves empty is silently
    skipped — preserves today's "credentials missing → MCP not
    spawned" semantics."""
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({
        "mcps": [_mcp(env={"TOKEN": "${MISSING}"})]
    }))
    plugins = load_plugins(p, env={})
    assert plugins.mcps == ()


def test_var_substitution_in_args(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({
        "mcps": [_mcp(args=["--config", "${CFG_PATH}"])]
    }))
    plugins = load_plugins(p, env={"CFG_PATH": "/etc/x.toml"})
    assert plugins.mcps[0].args == ("--config", "/etc/x.toml")


def test_unresolved_var_in_args_skips_mcp(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({
        "mcps": [_mcp(args=["--token", "${ABSENT}"])]
    }))
    plugins = load_plugins(p, env={})
    assert plugins.mcps == ()


def test_one_mcp_skipped_others_still_load(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({
        "mcps": [
            _mcp(name="needs-cred", env={"T": "${MISSING}"}),
            _mcp(name="no-cred"),
        ],
    }))
    plugins = load_plugins(p, env={})
    assert len(plugins.mcps) == 1
    assert plugins.mcps[0].name == "no-cred"


# ---------------------------------------------------------------------------
# Skills disabled
# ---------------------------------------------------------------------------


def test_skills_disabled_parsed(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"skills_disabled": ["render-style", "demo"]}))
    plugins = load_plugins(p)
    assert plugins.skills_disabled == frozenset({"render-style", "demo"})


def test_skills_disabled_filters_skills_store(tmp_path: Path) -> None:
    """End-to-end: a name in ``skills_disabled`` is hidden from
    :meth:`SkillsStore.list` even if its directory exists."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    for name in ("alpha", "beta"):
        d = skills_root / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: a test skill\n---\nbody\n"
        )

    listed = SkillsStore(skills_root).list()
    assert {s.name for s in listed} == {"alpha", "beta"}

    listed_filtered = SkillsStore(
        skills_root, disabled=frozenset({"beta"})
    ).list()
    assert {s.name for s in listed_filtered} == {"alpha"}


def test_builtin_tools_disabled_parsed(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"builtin_tools_disabled": ["create_poll", "render_html"]}))
    plugins = load_plugins(p)
    assert plugins.builtin_tools_disabled == frozenset({"create_poll", "render_html"})


def test_builtin_tools_disabled_non_string_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"builtin_tools_disabled": [123]}))
    with pytest.raises(PluginsConfigError, match="builtin_tools_disabled"):
        load_plugins(p)


def test_builtin_tools_disabled_filters_mcp_server(tmp_path: Path) -> None:
    """End-to-end: a name in ``builtin_tools_disabled`` is skipped at
    MCP registration time, so the tool simply doesn't exist on the
    server side and the model can't see or invoke it."""
    from pyclaudir.mcp_server import build_fastmcp, discover_tool_classes
    from pyclaudir.tools.base import ToolContext

    ctx = ToolContext(
        bot=None, database=None, memory_store=None, instructions_store=None,
        skills_store=None, attachment_store=None, render_store=None,
        chat_titles={},
    )

    all_names = {cls.name for cls in discover_tool_classes()}
    assert "create_poll" in all_names  # sanity

    _mcp_full, instances_full = build_fastmcp(ctx)
    full_names = {i.name for i in instances_full}
    assert "create_poll" in full_names

    _mcp_filtered, instances_filtered = build_fastmcp(
        ctx, disabled=frozenset({"create_poll", "stop_poll"}),
    )
    filtered_names = {i.name for i in instances_filtered}
    assert "create_poll" not in filtered_names
    assert "stop_poll" not in filtered_names
    # Other tools survive.
    assert "send_message" in filtered_names


def test_builtin_tools_disabled_unknown_name_crashes(tmp_path: Path) -> None:
    """A typo in ``builtin_tools_disabled`` (e.g. ``poll`` instead of
    ``create_poll``) must fail boot loudly with a list of available
    tool names — not silently do nothing."""
    from pyclaudir.mcp_server import build_fastmcp
    from pyclaudir.tools.base import ToolContext

    ctx = ToolContext(
        bot=None, database=None, memory_store=None, instructions_store=None,
        skills_store=None, attachment_store=None, render_store=None,
        chat_titles={},
    )
    with pytest.raises(ValueError, match="unknown name"):
        build_fastmcp(ctx, disabled=frozenset({"definitely_not_a_real_tool"}))


def test_skills_disabled_blocks_read(tmp_path: Path) -> None:
    """A disabled skill must not be readable either, so envelope-driven
    invocation (`<skill name="...">`) can't bypass the toggle."""
    from pyclaudir.skills_store import SkillsError

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    d = skills_root / "alpha"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: a test skill\n---\nbody\n"
    )

    store = SkillsStore(skills_root, disabled=frozenset({"alpha"}))
    with pytest.raises(SkillsError, match="not found"):
        store.read("alpha")


# ---------------------------------------------------------------------------
# Default plugins.json shipped at repo root parses cleanly
# ---------------------------------------------------------------------------


def test_repo_example_plugins_json_loads(tmp_path: Path) -> None:
    """The checked-in ``plugins.json.example`` at repo root must be
    valid JSON and conform to the schema. With no integration env vars
    set, every MCP should be skipped (matching today's "no creds = no
    MCP" boot)."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "plugins.json.example"
    assert example.exists(), "default plugins.json.example must be checked in"

    plugins = load_plugins(example, env={})
    assert plugins.mcps == ()  # no creds → all skipped
    assert plugins.tool_groups == {"bash": False, "code": False, "subagents": False}
    assert plugins.skills_disabled == frozenset()
    assert plugins.builtin_tools_disabled == frozenset()


def test_repo_example_plugins_json_with_creds(tmp_path: Path) -> None:
    """With Jira creds set, the mcp-atlassian plugin should resolve and
    advertise its 40-tool allowlist."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "plugins.json.example"

    plugins = load_plugins(
        example,
        env={
            "JIRA_URL": "https://x.atlassian.net",
            "JIRA_USERNAME": "u@x",
            "JIRA_API_TOKEN": "t",
        },
    )
    by_name = {m.name: m for m in plugins.mcps}
    assert "mcp-atlassian" in by_name
    assert len(by_name["mcp-atlassian"].allowed_tools) == 40
    assert by_name["mcp-atlassian"].env == {
        "JIRA_URL": "https://x.atlassian.net",
        "JIRA_USERNAME": "u@x",
        "JIRA_API_TOKEN": "t",
    }


def test_missing_file_with_example_present_logs_hint(tmp_path: Path, caplog) -> None:
    """When ``plugins.json`` is absent but ``plugins.json.example`` is
    present, the loader emits a WARNING with the cp command — points
    operators at the next step instead of leaving them to wonder why
    no MCPs spawned."""
    import logging
    example = tmp_path / "plugins.json.example"
    example.write_text("{}")
    target = tmp_path / "plugins.json"

    with caplog.at_level(logging.WARNING, logger="pyclaudir.plugins"):
        plugins = load_plugins(target)
    assert plugins == Plugins()
    assert any("cp" in r.message and "plugins.json.example" in r.message for r in caplog.records)
