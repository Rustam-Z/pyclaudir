"""Read-only memory tools.

These are the *only* file-reading tools Nodira gets in v1. There is no
``write_memory``, ``edit_memory``, or ``delete_memory`` — population is an
out-of-band operator job.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult


class ListMemoriesArgs(BaseModel):
    pass


class ListMemoriesTool(BaseTool):
    name = "list_memories"
    description = (
        "List every memory file the operator has placed under data/memories/. "
        "Returns one path per line with its size in bytes. Files are read-only "
        "from your perspective; the operator curates them out of band."
    )
    args_model = ListMemoriesArgs

    async def run(self, args: ListMemoriesArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="", is_error=True)
        files = store.list()
        if not files:
            return ToolResult(content="(no memory files)")
        lines = [f"{f.relative_path}\t{f.size_bytes}" for f in files]
        return ToolResult(
            content="\n".join(lines),
            data={"files": [f.relative_path for f in files]},
        )


class ReadMemoryArgs(BaseModel):
    path: str = Field(
        description="Relative path under data/memories/. No '..', no absolute paths.",
    )


class ReadMemoryTool(BaseTool):
    name = "read_memory"
    description = (
        "Read a memory file by relative path under data/memories/. UTF-8. "
        "Files larger than 64 KiB are truncated."
    )
    args_model = ReadMemoryArgs

    async def run(self, args: ReadMemoryArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)
        try:
            text = store.read(args.path)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(content=text, data={"path": args.path})
