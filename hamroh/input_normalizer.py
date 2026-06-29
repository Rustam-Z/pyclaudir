"""Normalize inbound user text to defang hidden-character injection.

Sibling of :mod:`secrets_scrubber`. Where the scrubber redacts credential
shapes for storage safety, this module strips Unicode obfuscation tricks
that an attacker might use to smuggle instructions past the model's
prompt-injection rules: zero-width characters, bidi controls,
soft hyphens, and NFKC-equivalent forms (homoglyphs / fullwidth /
compatibility characters).

The defense is intentionally narrow:

- We only strip characters that have **no legitimate role** in chat
  text — zero-width joiners, RTL/LTR overrides, byte-order marks. We
  keep regular RTL letters (Arabic, Hebrew) untouched; bidi controls
  are the part attackers abuse, not the script itself.
- We NFKC-normalize the entire string. This collapses fullwidth ASCII,
  ligatures, and most homoglyphs to their canonical forms.
- We **flag** what we changed via a returned :class:`frozenset`. The
  XML envelope surfaces these flags so the model can refuse on-character
  rather than guess at intent.

The robust defense against prompt injection still lives in the system
prompt (``prompts/system.md`` §Prompt-injection). This is cheap
defense-in-depth at the input boundary.
"""

from __future__ import annotations

import unicodedata

#: Zero-width / invisible characters frequently used to split keywords
#: or hide instructions ("ig​nore previous"). None of these have a
#: legitimate role in chat text we care about.
ZERO_WIDTH: frozenset[str] = frozenset(
    {
        "​",  # ZERO WIDTH SPACE
        "‌",  # ZERO WIDTH NON-JOINER
        "‍",  # ZERO WIDTH JOINER
        "⁠",  # WORD JOINER
        "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
        "­",  # SOFT HYPHEN
    }
)

#: Bidi formatting/override controls. These flip rendering direction
#: and let an attacker make on-screen text differ from logical text.
#: Plain RTL/LTR letters are NOT in this set — only the controls.
BIDI_CONTROLS: frozenset[str] = frozenset(
    {
        "‪",  # LEFT-TO-RIGHT EMBEDDING
        "‫",  # RIGHT-TO-LEFT EMBEDDING
        "‬",  # POP DIRECTIONAL FORMATTING
        "‭",  # LEFT-TO-RIGHT OVERRIDE
        "‮",  # RIGHT-TO-LEFT OVERRIDE
        "⁦",  # LEFT-TO-RIGHT ISOLATE
        "⁧",  # RIGHT-TO-LEFT ISOLATE
        "⁨",  # FIRST STRONG ISOLATE
        "⁩",  # POP DIRECTIONAL ISOLATE
    }
)

#: Flag values returned alongside cleaned text. Stable strings — the
#: system prompt and the XML envelope key off these literals.
FLAG_NFKC = "nfkc_changed"
FLAG_ZERO_WIDTH = "zero_width_stripped"
FLAG_BIDI = "bidi_stripped"


def normalize_inbound(text: str) -> tuple[str, frozenset[str]]:
    """Strip hidden-character injection vectors from ``text``.

    Returns ``(cleaned_text, flags)`` where ``flags`` names every
    transformation that actually fired. Empty / falsy input yields the
    original value and an empty flag set.
    """
    if not text:
        return text, frozenset()
    flags: set[str] = set()
    stripped_chars = ZERO_WIDTH | BIDI_CONTROLS
    if any(ch in stripped_chars for ch in text):
        kept = []
        for ch in text:
            if ch in ZERO_WIDTH:
                flags.add(FLAG_ZERO_WIDTH)
                continue
            if ch in BIDI_CONTROLS:
                flags.add(FLAG_BIDI)
                continue
            kept.append(ch)
        text = "".join(kept)
    nfkc = unicodedata.normalize("NFKC", text)
    if nfkc != text:
        flags.add(FLAG_NFKC)
        text = nfkc
    return text, frozenset(flags)
