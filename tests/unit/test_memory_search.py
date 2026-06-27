"""Memory content search: per-term matching, ranking, safety invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.storage.memory import MemoryPathError, MemoryStore
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.memory import SearchMemoryArgs, SearchMemoryTool


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path / "memories")
    s.ensure_root()
    return s


# ---------------------------------------------------------------------------
# search — matching
# ---------------------------------------------------------------------------


def test_search_finds_match_with_path_and_line(store: MemoryStore) -> None:
    """Given a keyword on a known line, search returns its path and line number."""
    (store.root / "notes.md").write_text("first line\nthe budget was approved\nlast")

    hits = store.search("budget")

    assert len(hits) == 1, "exactly one line should match 'budget'"
    assert hits[0].relative_path == "notes.md", "hit must report the file path"
    assert hits[0].line_number == 2, "match is on the second line"
    assert hits[0].line == "the budget was approved", "hit carries the matched line"


def test_search_spans_multiple_files(store: MemoryStore) -> None:
    """A keyword present in two files yields a hit from each."""
    (store.root / "a.md").write_text("auth uses JWT")
    (store.root / "b.md").write_text("auth deferred SSO")

    paths = {hit.relative_path for hit in store.search("auth")}

    assert paths == {"a.md", "b.md"}, "both files mentioning 'auth' must appear"


def test_search_is_case_insensitive(store: MemoryStore) -> None:
    """An uppercase query matches lowercase content and vice versa."""
    (store.root / "n.md").write_text("the Authentication flow")

    hits = store.search("AUTHENTICATION")

    assert len(hits) == 1, "case must not affect matching"


def test_search_matches_non_adjacent_terms(store: MemoryStore) -> None:
    """The whole-substring bug: terms need not be adjacent or in query order."""
    (store.root / "n.md").write_text("deadline for the Acme project")

    hits = store.search("acme deadline")

    assert len(hits) == 1, "'acme deadline' must find 'deadline for the Acme project'"
    assert hits[0].score == 2, "both distinct terms hit, so score is 2"


def test_search_ranks_more_terms_first(store: MemoryStore) -> None:
    """A line hitting more distinct terms sorts ahead of one hitting fewer."""
    (store.root / "n.md").write_text("only the deadline matters\nacme deadline today")

    hits = store.search("acme deadline")

    assert hits[0].line == "acme deadline today", "two-term line ranks first"
    assert hits[0].score == 2, "leading hit matched both terms"
    assert hits[1].score == 1, "trailing hit matched a single term"


def test_search_no_matches_returns_empty(store: MemoryStore) -> None:
    """A query absent from every file yields no hits."""
    (store.root / "n.md").write_text("nothing relevant here")

    assert store.search("absent") == [], "missing keyword must return no hits"


def test_search_blank_query_returns_empty(store: MemoryStore) -> None:
    """Whitespace-only queries have no terms and match nothing."""
    (store.root / "n.md").write_text("content")

    assert store.search("   ") == [], "blank query must short-circuit to empty"


def test_search_caps_results_keeping_best(store: MemoryStore) -> None:
    """max_results truncates, but the highest-scoring hits survive the cap."""
    lines = ["acme deadline"] + ["acme only"] * 5  # 1 two-term, 5 one-term
    (store.root / "n.md").write_text("\n".join(lines))

    hits = store.search("acme deadline", max_results=2)

    assert len(hits) == 2, "result count must respect max_results"
    assert hits[0].score == 2, "the best (two-term) hit must be kept"


# ---------------------------------------------------------------------------
# search — safety invariant
# ---------------------------------------------------------------------------


def test_search_does_not_unlock_read_before_write(store: MemoryStore) -> None:
    """Searching a file must NOT count as reading it for the write gate."""
    (store.root / "policy.md").write_text("CRITICAL config")

    store.search("config")  # matches policy.md but is not a "read"

    assert "policy.md" not in store.read_paths_snapshot, "search must not credit a read"
    with pytest.raises(MemoryPathError, match="read-before-write"):
        store.write("policy.md", "overwritten")


# ---------------------------------------------------------------------------
# tool wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_search_tool_happy_path(store: MemoryStore) -> None:
    """The MCP tool formats hits as 'path:line: text', best first."""
    (store.root / "n.md").write_text("acme deadline today\nacme only")
    tool = SearchMemoryTool(ToolContext(memory_store=store))

    result = await tool.run(SearchMemoryArgs(query="acme deadline"))

    assert result.is_error is False, "a successful search is not an error"
    assert result.content.startswith("n.md:1: acme deadline today"), (
        "output lists the best match first as path:line: text"
    )
    assert result.data == {"hits": ["n.md", "n.md"]}, "data carries the hit paths"


@pytest.mark.asyncio
async def test_memory_search_tool_no_matches(store: MemoryStore) -> None:
    """The tool reports an explicit empty result rather than erroring."""
    (store.root / "n.md").write_text("unrelated")
    tool = SearchMemoryTool(ToolContext(memory_store=store))

    result = await tool.run(SearchMemoryArgs(query="missing"))

    assert result.is_error is False, "no matches is a normal outcome, not an error"
    assert result.content == "(no matches)", "empty search states it plainly"
