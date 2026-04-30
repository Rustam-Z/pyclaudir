"""Access control for pyclaudir.

Hot-reloadable ``access.json`` (at the repo root, alongside
``plugins.json``) governs who can talk to the bot:

- **Policy**: ``owner_only`` (default), ``allowlist``, or ``open``.
  Governs both DMs and group chats.
- **Allowed users**: explicit list of Telegram user IDs (used in
  ``allowlist``).
- **Allowed chats**: explicit list of group/supergroup chat IDs (used
  in ``allowlist``).

Semantics:

- ``owner_only`` — only the owner in a DM. Groups are always blocked.
- ``allowlist`` — owner + ``allowed_users`` in DMs; groups in
  ``allowed_chats``.
- ``open`` — anyone in DMs, any group.

The owner (``PYCLAUDIR_OWNER_ID``) is always implicitly allowed in DMs
regardless of ``allowed_users`` content.

The file is re-read on every inbound message so edits take effect
immediately without a restart. Writes use atomic tmp+rename to prevent
corruption.
"""

from __future__ import annotations

import errno
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

Policy = Literal["owner_only", "allowlist", "open"]

_VALID_POLICIES: set[str] = {"owner_only", "allowlist", "open"}


@dataclass
class AccessConfig:
    policy: Policy = "owner_only"
    allowed_users: list[int] = field(default_factory=list)
    allowed_chats: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "allowed_users": self.allowed_users,
            "allowed_chats": self.allowed_chats,
        }


def load_access(path: Path) -> AccessConfig:
    """Read ``access.json``, returning defaults on missing or corrupt file."""
    if not path.exists():
        return AccessConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Corrupt file — rename aside so we don't keep hitting the error.
        import time

        aside = path.with_suffix(f".corrupt-{int(time.time())}.json")
        log.warning("corrupt access.json, renaming to %s: %s", aside.name, exc)
        try:
            path.rename(aside)
        except OSError:
            pass
        return AccessConfig()

    policy = raw.get("policy", "owner_only")
    if policy not in _VALID_POLICIES:
        log.warning("invalid policy %r in access.json, defaulting to owner_only", policy)
        policy = "owner_only"

    return AccessConfig(
        policy=policy,
        allowed_users=[int(u) for u in raw.get("allowed_users", []) if _is_int(u)],
        allowed_chats=[int(c) for c in raw.get("allowed_chats", []) if _is_int(c)],
    )


def save_access(path: Path, config: AccessConfig) -> None:
    """Atomically write ``access.json`` via tmp+rename.

    Falls back to in-place write when ``path`` is a single-file Docker
    bind mount, where ``rename(2)`` onto the mount target returns
    ``EBUSY``. ``load_access`` recovers from a torn write by renaming
    the corrupt file aside and returning defaults.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    try:
        tmp.rename(path)
    except OSError as exc:
        if exc.errno != errno.EBUSY:
            raise
        path.write_text(payload, encoding="utf-8")
        tmp.unlink(missing_ok=True)


def gate(
    *,
    access: AccessConfig,
    owner_id: int,
    chat_id: int,
    user_id: int,
    chat_type: str | None,
) -> bool:
    """Decide whether an inbound message should be forwarded to the engine.

    Returns ``True`` (deliver) or ``False`` (drop). Every message is
    persisted to SQLite regardless — this only controls whether the agent
    sees it.
    """
    is_group = chat_type in ("group", "supergroup", "channel")

    if access.policy == "owner_only":
        # Owner DMs only. Groups are always denied.
        return not is_group and user_id == owner_id

    if access.policy == "open":
        return True

    # "allowlist"
    if is_group:
        return chat_id in access.allowed_chats
    return user_id == owner_id or user_id in access.allowed_users


def _is_int(v) -> bool:
    if isinstance(v, int):
        return True
    if isinstance(v, str):
        try:
            int(v)
            return True
        except ValueError:
            return False
    return False
