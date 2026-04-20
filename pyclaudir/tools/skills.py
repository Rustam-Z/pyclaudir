"""Skill-file tools — list available skill playbooks and read a specific one.

Skills are operator-curated markdown playbooks under ``skills/<name>/SKILL.md``.
They are public reference material (not secrets) — no owner gate on
these tools. Access goes through :class:`pyclaudir.skills_store.SkillsStore`
which is strictly read-only and path-hardened.

The intended use: when a reminder injects a message of the form
``<skill name="X">run</skill>`` inside a ``<reminder>`` envelope, the
bot calls ``read_skill("X")`` and executes the playbook's steps.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult


class ListSkillsArgs(BaseModel):
    pass


class ListSkillsTool(BaseTool):
    name = "list_skills"
    description = (
        "List available agent skills (playbooks) under the project's "
        "skills/ directory, following the Agent Skills spec "
        "(https://agentskills.io/specification). Returns each skill's "
        "name and its frontmatter description — enough to decide which "
        "skill is relevant without loading the full body. Fetch the "
        "body via read_skill only when you're ready to execute. Skills "
        "are typically invoked via `<skill name=\"X\">run</skill>` "
        "inside a `<reminder>` envelope."
    )
    args_model = ListSkillsArgs

    async def run(self, args: ListSkillsArgs) -> ToolResult:
        store = self.ctx.skills_store
        if store is None:
            return ToolResult(content="skills store unavailable", is_error=True)
        files = store.list()
        if not files:
            return ToolResult(content="(no skills)")
        lines = [f"- **{f.name}** — {f.description}" for f in files]
        return ToolResult(
            content="\n".join(lines),
            data={
                "skills": [
                    {"name": f.name, "description": f.description}
                    for f in files
                ],
            },
        )


class ReadSkillArgs(BaseModel):
    name: str = Field(
        description="Skill name (e.g. 'self-reflection'). Must be a single directory name.",
    )


class ReadSkillTool(BaseTool):
    name = "read_skill"
    description = (
        "Read the playbook (SKILL.md) for a given agent skill. "
        "Returns the full markdown content. Call list_skills first if "
        "you're not sure what's available. Call this when a `<reminder>` "
        "envelope contains `<skill name=\"X\">run</skill>`."
    )
    args_model = ReadSkillArgs

    async def run(self, args: ReadSkillArgs) -> ToolResult:
        store = self.ctx.skills_store
        if store is None:
            return ToolResult(content="skills store unavailable", is_error=True)
        try:
            text = store.read(args.name)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(content=text, data={"name": args.name})
