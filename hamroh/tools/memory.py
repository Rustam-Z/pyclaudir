"""Memory tools — read, list, write, append.

This module is the **only** place in ``hamroh/tools/`` allowed to touch
the filesystem (security invariant 5). All four tools route through
:class:`hamroh.storage.memory.MemoryStore`, which enforces:

- path traversal protection (no ``..``, no absolute paths, no symlinks)
- 64 KiB per-file cap
- read-before-write for any file that already exists

There is intentionally no ``delete_memory`` tool. If the agent wants to
"forget" something it has to overwrite it; actually removing files
remains an operator-only action.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from ..storage.memory import MEMORY_TEMPLATE, MemoryFile
from .base import BaseTool, ToolResult


class ListMemoriesArgs(BaseModel):
    pass


class ListMemoriesTool(BaseTool[ListMemoriesArgs]):
    name = "memory_list"
    description = (
        "List every memory file under data/memories/, each with its "
        "frontmatter description — enough to decide which memory is relevant "
        "without reading the whole file (the same progressive-disclosure "
        "protocol skill_list uses). Memories carry name/description "
        "frontmatter; legacy files without it show just their path and size. "
        "To find files by their CONTENTS rather than their summaries, use "
        "memory_search instead."
    )
    args_model = ListMemoriesArgs

    async def run(self, args: ListMemoriesArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="", is_error=True)
        files = await asyncio.to_thread(store.list)
        if not files:
            return ToolResult(content="(no memory files)")
        lines = [_format_memory_line(f) for f in files]
        return ToolResult(
            content="\n".join(lines),
            data={
                "files": [
                    {"path": f.relative_path, "description": f.description}
                    for f in files
                ],
            },
        )


def _format_memory_line(f: MemoryFile) -> str:
    """Render one ``memory_list`` line: description when present, else path+size."""
    if f.description:
        return f"- {f.relative_path} — {f.description}"
    return f"- {f.relative_path}\t{f.size_bytes} bytes (no description)"


class SearchMemoryArgs(BaseModel):
    query: str = Field(
        description=(
            "Keywords to find inside memory file contents (case-insensitive). "
            "Use a few keywords, not a full sentence — e.g. 'acme deadline', "
            "not 'when is the Acme deadline?'. Lines matching more of your "
            "keywords rank higher."
        ),
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of matching lines to return.",
    )


class SearchMemoryTool(BaseTool[SearchMemoryArgs]):
    name = "memory_search"
    description = (
        "Search the TEXT INSIDE memory files (not just their names) for "
        "keywords. Case-insensitive. Returns matching lines as "
        "'path:line: text', best matches first. Faster than memory_list "
        "plus reading every file: search first, then memory_read the most "
        "relevant file for full context."
    )
    args_model = SearchMemoryArgs

    async def run(self, args: SearchMemoryArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)
        hits = await asyncio.to_thread(
            store.search, args.query, max_results=args.max_results
        )
        if not hits:
            return ToolResult(content="(no matches)")
        lines = [f"{h.relative_path}:{h.line_number}: {h.line}" for h in hits]
        return ToolResult(
            content="\n".join(lines),
            data={"hits": [h.relative_path for h in hits]},
        )


class ReadMemoryArgs(BaseModel):
    path: str = Field(
        description="Relative path under data/memories/. No '..', no absolute paths.",
    )


class ReadMemoryTool(BaseTool[ReadMemoryArgs]):
    name = "memory_read"
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
            text = await asyncio.to_thread(store.read, args.path)
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
        description=(
            "Full new file body. Overwrites any existing content. MUST begin "
            "with the frontmatter template:\n" + MEMORY_TEMPLATE + "\nKeep the "
            "description a fresh one-line summary of what the file now holds."
        ),
    )


class WriteMemoryTool(BaseTool[WriteMemoryArgs]):
    name = "memory_write"
    description = (
        "Create or fully overwrite a memory file. Max 64 KiB. Content MUST "
        "start with name/description frontmatter (the template) — writes "
        "without it are rejected — so memory_list can show what the file is "
        "about. Rewrite the description whenever the content changes. "
        "If the file already exists you MUST call memory_read on it first "
        "in the same session — a safety rail against destroying operator-"
        "curated notes. New files can be created without a prior read. Writes "
        "to local storage only — to send a memory file to the user, use "
        "telegram_send_memory_document."
    )
    args_model = WriteMemoryArgs

    async def run(self, args: WriteMemoryArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)
        try:
            written = await asyncio.to_thread(store.write, args.path, args.content)
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
        description="Text to append to the body. A trailing newline is NOT added.",
    )
    description: str = Field(
        description=(
            "Fresh one-line summary of what the file holds after this append. "
            "Replaces the file's frontmatter description so memory_list stays "
            "current. Max 1024 chars."
        ),
    )


class AppendMemoryTool(BaseTool[AppendMemoryArgs]):
    name = "memory_append"
    description = (
        "Append text to a memory file's body AND refresh its one-line "
        "description (kept in frontmatter) so memory_list always reflects the "
        "latest content. The file's name is preserved (or derived from the "
        "filename for a new/legacy file — the first append adds frontmatter). "
        "New total must stay under 64 KiB. If the file already exists you MUST "
        "call memory_read on it first in the same session. Useful for "
        "journals, running notes, conversation logs."
    )
    args_model = AppendMemoryArgs

    async def run(self, args: AppendMemoryArgs) -> ToolResult:
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)
        try:
            new_size = await asyncio.to_thread(
                store.append, args.path, args.content, args.description
            )
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=f"appended; {args.path} is now {new_size} bytes",
            data={"path": args.path, "bytes": new_size},
        )
