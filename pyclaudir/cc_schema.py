"""JSON Schema enforced on Claude Code's structured output."""

from __future__ import annotations

import json

#: Hard cap on ``reason``. At ~4 chars/token that's ~25 tokens worst case;
#: paired with the system-prompt nudge ("≤10 words, terse") a well-behaved
#: turn costs far less. Without a cap a single rambling justification can
#: burn 100+ tokens — cheap per turn, expensive over a long session.
REASON_MAX_LENGTH = 100

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
                "Required only when action == 'stop'."
            ),
        },
        "sleep_ms": {
            "type": ["integer", "null"],
            "description": "Only used when action == 'sleep'.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
    # Conditional: reason is required and must be non-empty when stopping.
    # For sleep/heartbeat it's optional — those actions are provisional,
    # not terminal, so the forcing-function argument doesn't apply.
    "allOf": [
        {
            "if": {
                "properties": {"action": {"const": "stop"}},
                "required": ["action"],
            },
            "then": {
                "required": ["reason"],
                "properties": {
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": REASON_MAX_LENGTH,
                    },
                },
            },
        },
    ],
}


def schema_json() -> str:
    return json.dumps(CONTROL_ACTION_SCHEMA)
