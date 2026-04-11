"""Memory tools — read, list, write, append.

This module is the **only** place in ``pyclaudir/tools/`` allowed to touch
the filesystem (security invariant 5). All four tools route through
:class:`pyclaudir.memory_store.MemoryStore`, which enforces:

- path traversal protection (no ``..``, no absolute paths, no symlinks)
- 64 KiB per-file cap
- read-before-write for any file that already exists

There is intentionally no ``delete_memory`` tool. If Nodira wants to
"forget" something she has to overwrite it; actually removing files
remains an operator-only action.
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
        "Files larger than 64 KiB are truncated. Reading a file unlocks "
        "subsequent writes/appends to it in this session."
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


class WriteMemoryArgs(BaseModel):
    path: str = Field(
        description=(
            "Relative path under data/memories/. No '..', no absolute paths. "
            "Parent directories are created automatically."
        ),
    )
    content: str = Field(
        description="Full new file body. Overwrites any existing content.",
    )


class WriteMemoryTool(BaseTool):
    name = "write_memory"
    description = (
        "Create or fully overwrite a memory file. Max 64 KiB. "
        "If the file already exists you MUST call read_memory on it first "
        "in the same session — this is a safety rail to stop accidental "
        "destruction of operator-curated notes. New files (that don't yet "
        "exist) can be created without a prior read."
    )
    args_model = WriteMemoryArgs

    async def run(self, args: WriteMemoryArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)
        try:
            written = store.write(args.path, args.content)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=f"wrote {written} bytes to {args.path}",
            data={"path": args.path, "bytes": written},
        )


class AppendMemoryArgs(BaseModel):
    path: str = Field(
        description="Relative path under data/memories/. No '..', no absolute paths.",
    )
    content: str = Field(
        description="Text to append. A trailing newline is NOT added automatically.",
    )


class AppendMemoryTool(BaseTool):
    name = "append_memory"
    description = (
        "Append text to a memory file. New total must stay under 64 KiB. "
        "If the file already exists you MUST call read_memory on it first "
        "in the same session. New files can be created without a prior "
        "read. Useful for journals, running notes, conversation logs."
    )
    args_model = AppendMemoryArgs

    async def run(self, args: AppendMemoryArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)
        try:
            new_size = store.append(args.path, args.content)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=f"appended; {args.path} is now {new_size} bytes",
            data={"path": args.path, "bytes": new_size},
        )
