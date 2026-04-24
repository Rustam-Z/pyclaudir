"""JSON Schema enforced on Claude Code's structured output."""

from __future__ import annotations

import json

#: Hard cap on ``reason``. At ~4 chars/token that's ~25 tokens worst case;
#: paired with the system-prompt nudge ("≤10 words, terse") a well-behaved
#: turn costs far less. Without a cap a single rambling justification can
#: burn 100+ tokens — cheap per turn, expensive over a long session.
REASON_MAX_LENGTH = 100

#: The Anthropic API's tool ``input_schema`` rejects top-level ``oneOf``,
#: ``allOf``, and ``anyOf`` (and likely ``if``/``then``). That means we
#: can't express "reason is required only on stop" in the schema itself
#: — this must stay a flat object. The "required on stop" invariant is
#: instead enforced client-side by :class:`~pyclaudir.models.ControlAction`'s
#: ``@model_validator`` when the stream-json event is parsed.
CONTROL_ACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["stop", "sleep", "heartbeat"],
            "description": "What to do after this turn.",
        },
        "reason": {
            "type": "string",
            "maxLength": REASON_MAX_LENGTH,
            "description": (
                "Terse justification (≤10 words). "
                "REQUIRED non-empty when action == 'stop'. "
                "Optional (may be omitted) when action is 'sleep' or 'heartbeat'."
            ),
        },
        "sleep_ms": {
            "type": ["integer", "null"],
            "description": "Only used when action == 'sleep'.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def schema_json() -> str:
    return json.dumps(CONTROL_ACTION_SCHEMA)
