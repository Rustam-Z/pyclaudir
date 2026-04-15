"""Replay or follow the bot's Claude Code session JSONL.

Claude Code persists every CC session as a JSONL file in
``~/.claude/projects/<project-cwd>/<session_id>.jsonl``. Each line is one
event: a user envelope, an assistant message (text + tool_use blocks),
synthetic user messages carrying tool_results, the final result, etc.

This script renders that file as a human-readable transcript of how the bot
processed each turn — useful for "what was it actually thinking when it
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


def _data_dir() -> Path:
    """Resolve the pyclaudir data dir without importing Config (which would
    require a Telegram token to validate). Match Config's logic exactly:
    PYCLAUDIR_DATA_DIR env var, default ``./data``.
    """
    raw = os.environ.get("PYCLAUDIR_DATA_DIR", "./data") or "./data"
    return Path(raw).resolve()


def _looks_like_bot_session(path: Path) -> bool:
    """A session is the bot's iff its first ``user`` event's text content
    starts with ``<msg `` — the XML envelope the engine wraps every
    Telegram batch in.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "user":
                    continue
                msg = event.get("message") or {}
                content = msg.get("content") or []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        return text.lstrip().startswith("<msg ")
                # First user event was a tool_result, not a text block —
                # keep scanning until we find an actual user-text event.
    except OSError:
        return False
    return False


def find_bot_session() -> Path | None:
    """Find the JSONL belonging to the bot, *not* the Claude Code session
    that happens to be running in the same project directory.

    Strategy:

    1. If ``data/session_id`` exists and the corresponding JSONL exists,
       use that. This is the canonical pyclaudir-self-reported session.
    2. Otherwise scan ``PROJECT_DIR`` for files whose first user event
       carries the bot's ``<msg ...>`` XML envelope, and return the most
       recently modified one.
    3. Otherwise return ``None``. (Don't fall back to "most-recent-of-any"
       because that's how we picked up the wrong session in the first
       place.)
    """
    if not PROJECT_DIR.exists():
        return None

    # 1. Authoritative: data/session_id
    sid_file = _data_dir() / "session_id"
    if sid_file.exists():
        sid = sid_file.read_text().strip()
        if sid:
            candidate = PROJECT_DIR / f"{sid}.jsonl"
            if candidate.exists():
                return candidate

    # 2. Fingerprint scan
    candidates = sorted(
        PROJECT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if _looks_like_bot_session(path):
            return path
    return None


def find_latest_session() -> Path | None:
    """Most recently modified file in the project dir, regardless of owner."""
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
    parser = argparse.ArgumentParser(
        description=(
            "Replay or tail the bot's CC session. By default picks the bot's "
            "session via data/session_id, NOT the most-recent file (which "
            "might be your own Claude Code session in the same cwd)."
        ),
    )
    parser.add_argument(
        "--session", "-s",
        help="Specific session id (overrides --latest and the bot session finder)",
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
    parser.add_argument(
        "--latest", action="store_true",
        help=(
            "Use the most recently modified JSONL regardless of owner. "
            "Useful when you DO want to follow this CC session itself."
        ),
    )
    args = parser.parse_args()

    if args.list:
        if not PROJECT_DIR.exists():
            print(f"no session dir at {PROJECT_DIR}")
            return 1
        # Determine which file is the bot's so we can mark it
        bot_path = find_bot_session()
        bot_stem = bot_path.stem if bot_path else None
        files = sorted(
            PROJECT_DIR.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in files:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
            size_kb = p.stat().st_size // 1024
            marker = "  ← bot" if p.stem == bot_stem else ""
            print(f"{mtime}  {size_kb:>6} KB  {p.stem}{marker}")
        return 0

    if args.session:
        path = find_session_by_id(args.session)
        if path is None:
            print(f"no session file for id {args.session} under {PROJECT_DIR}", file=sys.stderr)
            return 1
    elif args.latest:
        path = find_latest_session()
        if path is None:
            print(f"no session files under {PROJECT_DIR}", file=sys.stderr)
            return 1
    else:
        path = find_bot_session()
        if path is None:
            print(
                "could not identify the bot's session. Try one of:\n"
                "  --session <id>          to specify it explicitly\n"
                "  --list                  to see all available sessions\n"
                "  --latest                to use the most recent file regardless\n"
                f"  data/session_id under {_data_dir()} is empty/missing.",
                file=sys.stderr,
            )
            return 1

    if args.follow:
        follow(path, args.max)
    else:
        replay(path, args.max)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
