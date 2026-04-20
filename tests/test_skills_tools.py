"""MCP skill tools — list_skills and read_skill surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.skills_store import SkillsStore
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.skills import (
    ListSkillsArgs,
    ListSkillsTool,
    ReadSkillArgs,
    ReadSkillTool,
)


_VALID = """---
name: self-reflection
description: A test skill for tool-level unit testing. This is what list_skills surfaces.
---

# self-reflection

Body.
"""


def _store(tmp_path: Path) -> SkillsStore:
    root = tmp_path / "skills"
    (root / "self-reflection").mkdir(parents=True)
    (root / "self-reflection" / "SKILL.md").write_text(_VALID)
    s = SkillsStore(root=root)
    s.ensure_root()
    return s


def _ctx(store: SkillsStore) -> ToolContext:
    return ToolContext(skills_store=store)


@pytest.mark.asyncio
async def test_list_skills_returns_names_and_descriptions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store)
    result = await ListSkillsTool(ctx).run(ListSkillsArgs())
    assert result.is_error is False
    assert "self-reflection" in result.content
    assert "tool-level unit testing" in result.content
    assert result.data is not None
    assert result.data["skills"] == [
        {
            "name": "self-reflection",
            "description": "A test skill for tool-level unit testing. "
                           "This is what list_skills surfaces.",
        },
    ]


@pytest.mark.asyncio
async def test_list_skills_empty(tmp_path: Path) -> None:
    store = SkillsStore(root=tmp_path / "empty")
    store.ensure_root()
    ctx = _ctx(store)
    result = await ListSkillsTool(ctx).run(ListSkillsArgs())
    assert result.is_error is False
    assert result.content == "(no skills)"


@pytest.mark.asyncio
async def test_read_skill_returns_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store)
    result = await ReadSkillTool(ctx).run(ReadSkillArgs(name="self-reflection"))
    assert result.is_error is False
    assert result.content.startswith("---")
    assert "name: self-reflection" in result.content


@pytest.mark.asyncio
async def test_read_skill_unknown_name_is_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store)
    result = await ReadSkillTool(ctx).run(ReadSkillArgs(name="does-not-exist"))
    assert result.is_error is True
    assert "skill not found" in result.content


@pytest.mark.asyncio
async def test_read_skill_traversal_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store)
    result = await ReadSkillTool(ctx).run(ReadSkillArgs(name="../secrets"))
    assert result.is_error is True


@pytest.mark.asyncio
async def test_tools_handle_missing_store(tmp_path: Path) -> None:
    ctx = ToolContext(skills_store=None)
    r1 = await ListSkillsTool(ctx).run(ListSkillsArgs())
    assert r1.is_error is True
    r2 = await ReadSkillTool(ctx).run(ReadSkillArgs(name="x"))
    assert r2.is_error is True
