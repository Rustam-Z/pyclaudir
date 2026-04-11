"""JSON Schema enforced on Claude Code's structured output."""

from __future__ import annotations

import json

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
            "minLength": 1,
            "description": "Required justification for the chosen action.",
        },
        "sleep_ms": {
            "type": ["integer", "null"],
            "description": "Only used when action == 'sleep'.",
        },
    },
    "required": ["action", "reason"],
    "additionalProperties": False,
}


def schema_json() -> str:
    return json.dumps(CONTROL_ACTION_SCHEMA)
