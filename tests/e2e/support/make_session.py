"""One-time: capture the tester account's Telethon session for the e2e suite.

Run in YOUR terminal — it logs you into Telegram (phone number, the code
Telegram sends you, and your 2FA password if set):

    .venv/bin/python tests/e2e/make_session.py

It reads E2E_TG_API_ID / E2E_TG_API_HASH from the project root ``.env`` (or
prompts if they're missing), logs in, then writes E2E_TG_SESSION and
HAMROH_OWNER_ID back into that file — preserving every other line. Fill
E2E_BOT_USERNAME in by hand. The test group comes from access.json
allowed_chats, not the env.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _key_of(line: str) -> str | None:
    """The env-var name on this line, or ``None`` for a comment/blank line."""
    if "=" not in line or line.lstrip().startswith("#"):
        return None
    return line.split("=", 1)[0].strip()


def _merge_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set ``updates`` in ``path``, updating keys in place and appending the
    rest — every other line (comments, app vars) is preserved untouched."""
    pending = dict(updates)
    out: list[str] = []
    for line in path.read_text().splitlines() if path.exists() else []:
        key = _key_of(line)
        out.append(f"{key}={pending.pop(key)}" if key in pending else line)
    out.extend(f"{k}={v}" for k, v in pending.items())
    path.write_text("\n".join(out) + "\n")


async def _login(api_id: int, api_hash: str) -> tuple[int, str]:
    """Interactive login; returns ``(user_id, session_string)``."""
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # prompts phone, code, 2FA via input()
    try:
        me = await client.get_me()
        return int(me.id), str(client.session.save())
    finally:
        await client.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    saved = _read_env_file(ENV_FILE)
    api_id_raw = os.environ.get("E2E_TG_API_ID") or saved.get("E2E_TG_API_ID")
    api_id = int(api_id_raw or input("api_id: "))
    api_hash = (
        os.environ.get("E2E_TG_API_HASH")
        or saved.get("E2E_TG_API_HASH")
        or input("api_hash: ")
    )
    log.info("Logging in — enter your phone number (e.g. +998901234567) when asked.")
    user_id, session = asyncio.run(_login(api_id, api_hash))

    updates = {
        "E2E_TG_API_ID": str(api_id),
        "E2E_TG_API_HASH": api_hash,
        "E2E_TG_SESSION": session,
        "HAMROH_OWNER_ID": str(user_id),
    }
    # Placeholder the operator fills by hand; keep any value already set.
    updates["E2E_BOT_USERNAME"] = saved.get("E2E_BOT_USERNAME", "")
    _merge_env_file(ENV_FILE, updates)
    log.info(
        "Wrote E2E_TG_SESSION + HAMROH_OWNER_ID (your id=%s) to %s",
        user_id,
        ENV_FILE,
    )
    log.info("You can now run:  .venv/bin/python -m pytest tests/e2e -m e2e -v")


if __name__ == "__main__":
    main()
