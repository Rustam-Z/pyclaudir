"""Classify CC-side failure signals into user-facing messages.

Data-driven: add a new failure mode by appending a :class:`CcFailurePattern`
to :data:`CC_FAILURE_PATTERNS`. Detectors in :mod:`pyclaudir.engine` and the
``_on_cc_crash`` hook in ``__main__.py`` both consume the classifier so there
is one authoritative mapping from "CC diagnostic text" to "what the user
sees", instead of scattered substring checks across the control loop.

Matches are case-insensitive and substring-based. Patterns are checked in
order and the first match wins, so place more-specific patterns before
generic ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class CcFailurePattern:
    """One known failure signature.

    :attr kind: short stable identifier for logs / tests (e.g. ``"auth"``).
    :attr keywords: substrings (any one match triggers the pattern). All
        compared lowercase, so provide lowercase here.
    :attr user_message: the Telegram-facing message. Kept short and action-
        oriented; operator-specific detail (env-var names, paths) is fine
        because the owner is typically the one reading it.
    """

    kind: str
    keywords: tuple[str, ...]
    user_message: str


#: Ordered from most specific to most generic. First match wins.
CC_FAILURE_PATTERNS: tuple[CcFailurePattern, ...] = (
    CcFailurePattern(
        kind="model-access",
        keywords=(
            "issue with the selected model",
            "model does not exist",
            "model not found",
            "you may not have access to it",
        ),
        user_message=(
            "⚠️ The configured Claude model is unavailable. "
            "The operator needs to fix `PYCLAUDIR_MODEL` in the environment."
        ),
    ),
    CcFailurePattern(
        kind="auth",
        keywords=(
            "unauthorized",
            "authentication failed",
            "invalid api key",
            "invalid_api_key",
            "subscription",
            "please log in",
            "not logged in",
            "expired credentials",
            "expired token",
        ),
        user_message=(
            "⚠️ Claude authentication failed — the API key or subscription "
            "may have expired. The operator needs to re-authenticate "
            "(`claude login` or update the key)."
        ),
    ),
    CcFailurePattern(
        kind="quota",
        keywords=(
            "quota",
            "usage limit",
            "credits exhausted",
            "insufficient credits",
        ),
        user_message=(
            "⚠️ Claude API quota/credits exhausted. "
            "The operator needs to top up or wait for the quota to reset."
        ),
    ),
    CcFailurePattern(
        kind="rate-limit",
        keywords=("rate limit", "rate_limit", "overloaded", "too many requests"),
        user_message=(
            "⚠️ I've been rate-limited by the API. "
            "Give me a moment and try again shortly."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CcFailureClassification:
    """The result of classifying CC diagnostic text."""

    kind: str
    user_message: str
    matched_source: str  # the substring of the source where the pattern matched


def classify_cc_failure(
    sources: Iterable[str],
    patterns: tuple[CcFailurePattern, ...] = CC_FAILURE_PATTERNS,
) -> CcFailureClassification | None:
    """Return the first failure classification matching any source text.

    ``sources`` can be any mix of stderr lines, text blocks, or other CC
    output — the classifier doesn't care about line structure, it just
    substring-matches lowercased pattern keywords. Returns ``None`` if no
    pattern matches.
    """
    for raw in sources:
        if not raw:
            continue
        haystack = raw.lower()
        for pattern in patterns:
            for kw in pattern.keywords:
                if kw in haystack:
                    # Truncate the matched source for the caller's
                    # logging / UI needs. Keep it small enough to embed
                    # in a Telegram message without blowing size limits.
                    snippet = raw.strip()
                    if len(snippet) > 400:
                        snippet = snippet[:400].rstrip() + "…"
                    return CcFailureClassification(
                        kind=pattern.kind,
                        user_message=pattern.user_message,
                        matched_source=snippet,
                    )
    return None
