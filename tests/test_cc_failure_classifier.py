"""Unit tests for :mod:`pyclaudir.cc_failure_classifier`.

Data-driven classifier: matches CC stderr / text blocks against known
failure keywords and returns a user-facing message. These tests pin the
contract so that adding / tweaking a pattern is an explicit, visible
change.
"""

from __future__ import annotations

import pytest

from pyclaudir.cc_failure_classifier import (
    CC_FAILURE_PATTERNS,
    CcFailureClassification,
    CcFailurePattern,
    classify_cc_failure,
)


def test_patterns_are_nonempty_and_lowercase() -> None:
    """Every keyword must be lowercase (matching is case-insensitive on
    the *haystack* side only) and every pattern must have at least one
    keyword and a user message."""
    assert len(CC_FAILURE_PATTERNS) > 0
    for pattern in CC_FAILURE_PATTERNS:
        assert isinstance(pattern, CcFailurePattern)
        assert pattern.kind, "pattern kind must be non-empty"
        assert pattern.keywords, f"pattern {pattern.kind!r} has no keywords"
        assert pattern.user_message, f"pattern {pattern.kind!r} has no user message"
        for kw in pattern.keywords:
            assert kw == kw.lower(), (
                f"keyword {kw!r} in {pattern.kind!r} must be lowercase "
                "(matching is lowercase-haystack)"
            )


def test_kinds_are_unique() -> None:
    """Two patterns sharing a `kind` would make log/test assertions
    ambiguous."""
    kinds = [p.kind for p in CC_FAILURE_PATTERNS]
    assert len(kinds) == len(set(kinds)), f"duplicate kinds: {kinds}"


def test_no_match_returns_none() -> None:
    assert classify_cc_failure([]) is None
    assert classify_cc_failure(["hello world", "some unrelated log line"]) is None
    assert classify_cc_failure([""]) is None


def test_model_access_pattern_matches_sonnet_typo() -> None:
    sources = [
        "There's an issue with the selected model (claude-sonnet-4-7). "
        "It may not exist or you may not have access to it."
    ]
    result = classify_cc_failure(sources)
    assert result is not None
    assert result.kind == "model-access"
    assert "pyclaudir_model" in result.user_message.lower()
    assert "claude-sonnet-4-7" in result.matched_source


def test_auth_pattern_catches_common_phrasings() -> None:
    for line in (
        "Error: Unauthorized",
        "authentication failed for API key",
        "Invalid API key",
        "Your subscription has expired",
        "Please log in with `claude login`",
        "invalid_api_key: credential rejected",
    ):
        result = classify_cc_failure([line])
        assert result is not None, f"missed auth line: {line!r}"
        assert result.kind == "auth"


def test_quota_pattern() -> None:
    result = classify_cc_failure(["You have exhausted your API quota for today."])
    assert result is not None
    assert result.kind == "quota"


def test_rate_limit_pattern() -> None:
    result = classify_cc_failure(["429 Too Many Requests — rate limit exceeded"])
    assert result is not None
    assert result.kind == "rate-limit"


def test_first_match_wins_across_multiple_sources() -> None:
    """When multiple sources would match different patterns, the FIRST
    source (by iteration order) that matches any pattern wins — keyed
    on source order, not pattern-table order."""
    sources = [
        "subscription expired",
        "overloaded",
    ]
    result = classify_cc_failure(sources)
    assert result is not None
    assert result.kind == "auth"


def test_matched_source_truncated_to_400_chars() -> None:
    long_source = "unauthorized " + ("x" * 1000)
    result = classify_cc_failure([long_source])
    assert result is not None
    assert len(result.matched_source) <= 401  # 400 + trailing ellipsis char
    assert result.matched_source.endswith("…")


def test_case_insensitive_haystack() -> None:
    result = classify_cc_failure(["UNAUTHORIZED: request rejected"])
    assert result is not None
    assert result.kind == "auth"


def test_custom_patterns_argument() -> None:
    """Callers (e.g. tests or a future DI) can pass their own pattern list."""
    custom = (
        CcFailurePattern(
            kind="custom",
            keywords=("banana",),
            user_message="banana failure",
        ),
    )
    result = classify_cc_failure(["I ate a banana"], patterns=custom)
    assert result is not None
    assert result.kind == "custom"
    assert result.user_message == "banana failure"


def test_classification_is_frozen() -> None:
    c = CcFailureClassification(kind="x", user_message="y", matched_source="z")
    with pytest.raises((AttributeError, TypeError)):
        c.kind = "other"  # type: ignore[misc]
