"""Replay or follow Nodira's Claude Code session JSONL.

Claude Code persists every CC session as a JSONL file in
``~/.claude/projects/<project-cwd>/<session_id>.jsonl``. Each line is one
event: a user envelope, an assistant message (text + tool_use blocks),
synthetic user messages carrying tool_results, the final result, etc.

This script renders that file as a human-readable transcript of how Nodira
processed each turn — useful for "what was she actually thinking when she
replied with X?".

Usage::

    # Print the most recent session, full
    uv run python -m pyclaudir.scripts.trace

    # Specific session id
    uv run python -m pyclaudir.scripts.trace --session 87f472fa-...

    # Tail it live (like `tail -f`)
    uv run python -m pyclaudir.scripts.trace --follow

    # Truncate long blocks (default: full text)
    uv run python -m pyclaudir.scripts.trace --max 200

The script is read-only and never touches the running pyclaudir process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_DIR = (
    Path.home()
    / ".claude"
    / "projects"
    / "-Users-rustam-z-development-agents-yalla"
)


def find_latest_session() -> Path | None:
    if not PROJECT_DIR.exists():
        return None
    candidates = sorted(
        PROJECT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def find_session_by_id(session_id: str) -> Path | None:
    p = PROJECT_DIR / f"{session_id}.jsonl"
    return p if p.exists() else None


def trunc(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"…[+{len(text) - max_chars}]"


def render_event(event: dict, max_chars: int) -> list[str]:
    """Return zero or more pretty-printed lines for one JSONL event."""
    out: list[str] = []
    etype = event.get("type")

    # Skip internal queue/init bookkeeping; they're noise for a transcript.
    if etype in {"queue-operation", "summary", "system"}:
        return out

    ts = event.get("timestamp", "")[:19].replace("T", " ")

    if etype == "user":
        msg = event.get("message", {}) or {}
        for block in msg.get("content") or []:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "text":
                txt = block.get("text", "")
                if txt.startswith("<msg "):
                    out.append(f"{ts}  ← user (telegram batch)")
                    for line in txt.splitlines():
                        out.append(f"             {trunc(line, max_chars)}")
                elif txt.startswith("<error>"):
                    out.append(f"{ts}  ← engine correction: {trunc(txt, max_chars)}")
                else:
                    out.append(f"{ts}  ← user: {trunc(txt, max_chars)}")
            elif btype == "tool_result":
                raw = block.get("content")
                if isinstance(raw, list):
                    text = " ".join(
                        (b.get("text", "") if isinstance(b, dict) else str(b))
                        for b in raw
                    )
                else:
                    text = "" if raw is None else str(raw)
                err = " ✗" if block.get("is_error") else " ✓"
                tid = str(block.get("tool_use_id", ""))[:8]
                out.append(f"{ts}  ← tool_result{err} id={tid}: {trunc(text, max_chars)}")
        return out

    if etype == "assistant":
        msg = event.get("message", {}) or {}
        for block in msg.get("content") or []:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "text":
                txt = block.get("text", "")
                if txt:
                    out.append(f"{ts}  → assistant text: {trunc(txt, max_chars)}")
            elif btype == "thinking":
                txt = block.get("thinking", "")
                out.append(f"{ts}  → thinking: {trunc(txt, max_chars)}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                tid = str(block.get("id", ""))[:8]
                args = block.get("input", {})
                try:
                    args_str = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args_str = str(args)
                out.append(
                    f"{ts}  → tool_use: {name}({trunc(args_str, max_chars)}) id={tid}"
                )
        return out

    if etype == "result":
        result = event.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                pass
        if isinstance(result, dict):
            action = result.get("action", "?")
            reason = result.get("reason", "")
            out.append(f"{ts}  ✦ turn done: action={action} reason={trunc(reason, max_chars)}")
        else:
            out.append(f"{ts}  ✦ turn done: {trunc(str(result), max_chars)}")
        out.append("")  # blank line between turns
        return out

    return out


def replay(path: Path, max_chars: int) -> None:
    print(f"# session: {path.name}")
    print(f"# file:    {path}")
    print(f"# size:    {path.stat().st_size} bytes")
    print()
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for line in render_event(event, max_chars):
                print(line)


def follow(path: Path, max_chars: int) -> None:
    print(f"# following: {path.name} (Ctrl+C to stop)")
    print()
    # First, print whatever's already in the file
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for line in render_event(event, max_chars):
                print(line)
        # Then keep tailing
        try:
            while True:
                where = fh.tell()
                line = fh.readline()
                if not line:
                    time.sleep(0.5)
                    fh.seek(where)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for rendered in render_event(event, max_chars):
                    print(rendered)
                sys.stdout.flush()
        except KeyboardInterrupt:
            print()
            print("# stopped following")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay or tail Nodira's CC session.")
    parser.add_argument(
        "--session", "-s",
        help="Session id (default: most-recent file in the project dir)",
    )
    parser.add_argument(
        "--follow", "-f", action="store_true",
        help="Tail the file like `tail -f`.",
    )
    parser.add_argument(
        "--max", type=int, default=0,
        help="Truncate long text blocks to N chars (0 = unlimited)",
    )
    parser.add_argument(
        "--list", "-l", action="store_true",
        help="List available session files and exit.",
    )
    args = parser.parse_args()

    if args.list:
        if not PROJECT_DIR.exists():
            print(f"no session dir at {PROJECT_DIR}")
            return 1
        files = sorted(
            PROJECT_DIR.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in files:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
            size_kb = p.stat().st_size // 1024
            print(f"{mtime}  {size_kb:>6} KB  {p.stem}")
        return 0

    if args.session:
        path = find_session_by_id(args.session)
        if path is None:
            print(f"no session file for id {args.session} under {PROJECT_DIR}", file=sys.stderr)
            return 1
    else:
        path = find_latest_session()
        if path is None:
            print(f"no session files under {PROJECT_DIR}", file=sys.stderr)
            return 1

    if args.follow:
        follow(path, args.max)
    else:
        replay(path, args.max)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
