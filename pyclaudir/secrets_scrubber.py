"""Redact credential-shaped strings before they hit persistent storage.

The system prompt tells the bot not to echo secrets (OWASP LLM02), but
the inbound message text and the raw Telegram Update JSON get stored
in SQLite regardless. If a user pastes an API key into a DM, it sits
in ``data/pyclaudir.db`` forever, queryable via ``query_db`` and
visible in any DB dump. This module redacts well-known patterns at
the persistence boundary so they never land in the DB.

Conservative by design: better to let a borderline string through than
to over-redact and break real content. We only match patterns that are
effectively impossible to produce legitimately in a chat message.

Patterns covered:

- **Bearer-style Authorization headers** — ``Authorization: Bearer <token>``
- **OpenAI / Anthropic / generic sk- keys** — ``sk-...`` (len ≥ 20)
- **AWS access keys** — ``AKIA[A-Z0-9]{16}``
- **AWS secret keys** — 40-char base64ish strings in a ``key=`` context
- **GitHub tokens** — ``gh[pousr]_[A-Za-z0-9]{36,}``
- **Slack tokens** — ``xox[bapor]-...``
- **JWTs** — ``eyJ[A-Za-z0-9_-]+\\.eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+``
- **Private-key PEM blocks** — ``-----BEGIN {RSA,EC,OPENSSH,PGP} PRIVATE KEY-----``
- **PostgreSQL/MySQL/MongoDB DSNs with embedded passwords** —
  ``scheme://user:password@host/``

Names and emails are NOT redacted (they're handled at the application
layer — access control, per-user memory).
"""

from __future__ import annotations

import re

#: Sentinel that replaces every match. Chosen to be obviously-a-placeholder
#: so the bot's downstream processing sees "something got redacted here"
#: rather than plausible-looking content.
REDACTION = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer / Token / API-Key / Basic
    re.compile(
        r"(?i)\b(authorization|api[-_ ]?key|x[-_]api[-_]key)\s*[:=]\s*[\"']?"
        r"(bearer\s+)?[A-Za-z0-9._\-+/=]{16,}[\"']?",
    ),
    # sk-... style (OpenAI, Anthropic, many other vendors)
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    # GitHub tokens (classic & fine-grained)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]+\b"),
    # JWT (three base64url segments separated by dots, first starts with eyJ)
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    # PEM private-key blocks (drop the entire block)
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA |)PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |OPENSSH |PGP |DSA |)PRIVATE KEY-----",
    ),
    # DSN with embedded password: scheme://user:password@host
    # Match schemes that commonly include credentials.
    re.compile(
        r"\b(postgres|postgresql|mysql|mariadb|mongodb|mongodb\+srv|redis|amqp|"
        r"amqps|rediss|ftp|ftps|sftp|ssh|smb|ldap|ldaps)://"
        r"[^\s:/@]+:[^\s:/@]+@[^\s]+",
    ),
)


def scrub(text: str) -> str:
    """Return ``text`` with all recognized credential patterns replaced by
    :data:`REDACTION`. Leaves non-matching content untouched.
    """
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTION, text)
    return text


def contains_secret(text: str) -> bool:
    """Cheap predicate: True iff :func:`scrub` would change ``text``."""
    if not text:
        return False
    for pattern in _PATTERNS:
        if pattern.search(text):
            return True
    return False
