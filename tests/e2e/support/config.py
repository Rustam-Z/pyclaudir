"""Configuration for an e2e run: env vars (``E2EConfig``) and the
response-time budgets every feature test asserts against.

Reply limits check the first chunk (``t_first_s``); the others bound how long
an observable (reaction, linkage row, full burst, fired reminder) takes to
appear.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from hamroh.access import load_access

#: The project's root ``.env`` — the single source of truth for both the app
#: and the e2e suite (the SUT inherits it; see ``child_env``).
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

#: The root ``access.json`` the SUT reads — also the source of the test group(s).
ACCESS_FILE = Path(__file__).resolve().parents[3] / "access.json"

#: Vars the e2e suite needs present (drives skip-gating). The first group is
#: tester-client only (no app equivalent); the rest are the app vars the SUT
#: itself consumes — listing them means a bare ``.env`` still skips cleanly.
#: The test group is not here — it comes from ``access.json`` (see ``group_ids``).
_REQUIRED_ENV = (
    "E2E_TG_API_ID",
    "E2E_TG_API_HASH",
    "E2E_TG_SESSION",
    "E2E_BOT_USERNAME",
    "TELEGRAM_BOT_TOKEN",
    "HAMROH_OWNER_ID",
    "HAMROH_MODEL",
    "HAMROH_EFFORT",
)

#: The status heartbeat interval for the dedicated ``status_sut`` bot only
#: (operator default is 300s). NOT in ``SUT_ENV_OVERRIDES`` — it reaches the bot
#: solely through the ``status_sut`` fixture's ``extra_env`` (see ``STATUS_SUT_ENV``
#: and ``conftest.py``), so every other test keeps the production interval and
#: never sees a "still working" ping mid-turn. Kept small so the test is quick:
#: the parked turn only has to outlive this.
STATUS_SUT_INTERVAL_S = 10.0

#: The env override the ``status_sut`` fixture applies — and nothing else does.
STATUS_SUT_ENV: dict[str, str] = {
    "HAMROH_STATUS_INTERVAL_SECONDS": str(int(STATUS_SUT_INTERVAL_S))
}

#: The env override the ``draft_sut`` fixture applies — and nothing else does.
#: Turns on the live "working…" progress draft (``sendMessageDraft``) so the
#: draft-mode regression test can confirm DM replies still land unaffected.
DRAFT_SUT_ENV: dict[str, str] = {"HAMROH_PROGRESS_DRAFT_ENABLED": "true"}

#: Overrides applied over the operator's ``.env`` for the SUT (see ``child_env``).
#: ``HAMROH_EFFORT="low"`` is pinned so turn latency stays fast and consistent
#: regardless of the operator's setting — the per-test latency gates flake when a
#: high-effort turn lands in the slow tail. Add e.g.
#: ``HAMROH_RATE_LIMIT_PER_MIN="120"`` here if the burst test hits the default.
SUT_ENV_OVERRIDES: dict[str, str] = {"HAMROH_EFFORT": "low"}

_QUIET_WINDOW_S = 3.0  # silence that marks a multi-chunk reply as complete
_BURST_TIMEOUT_S = 90.0  # how long to wait for every burst reply to land
_MULTI_MSG_TIMEOUT_S = 30.0  # how long to wait for every split-reply message to land

MAX_TEXT_REPLY_S = 15.0  # a plain text answer
MAX_BURST_S = 30.0  # every reply to a 3-message burst lands
MAX_MEMORY_REPLY_S = 30.0  # a turn that writes/reads a memory file
MAX_SKILL_REPLY_S = 30.0  # a turn that reads a skill first
MAX_REMINDER_REPLY_S = 30.0  # scheduling a reminder (reads the reminder-format skill)
MAX_REMINDER_FIRE_S = 60.0  # a scheduled reminder actually fires (delayed)
MAX_RENDER_REPLY_S = 60.0  # a turn that renders an image
MAX_BROWSER_REPLY_S = (
    120.0  # a multi-step browser flow may emit progress msgs; wait ≤2min for the photo
)
MAX_RESET_REPLY_S = 15.0  # /reset_session respawns the engine (MCP-class bound)
MAX_KILL_S = 15.0  # the bot process exits after /kill

# Hearthbeat / status checks: the SUT's own interval is 300s, but the dedicated
# ``status_sut`` fixture squeezes it to 10s so the parked turn can observe a heartbeat mid-turn.
MAX_STATUS_PING_S = (
    STATUS_SUT_INTERVAL_S + 5
)  # first heartbeat: fires AT the 10s interval, so it lands just after it
MAX_STATUS_TURN_S = (
    60.0  # a turn parked in one short browser wait, then its done-marker
)


def load_env() -> None:
    """Load the project root ``.env`` so a run needs no manual ``export``.

    Real environment variables win over the file (``override=False``); a
    missing file is a no-op.
    """
    load_dotenv(ENV_FILE)


def missing_env() -> list[str]:
    """Names of required env vars that are unset (drives skip-gating)."""
    return [name for name in _REQUIRED_ENV if not os.environ.get(name)]


def group_ids() -> list[int]:
    """The test group(s), taken from ``access.json`` ``allowed_chats``.

    One group means every group test uses it; several are round-robined across
    tests by the ``group_id`` fixture. Raises when none are configured — a group
    test can't run without an authorized group.
    """
    chats = load_access(ACCESS_FILE).allowed_chats
    if not chats:
        raise RuntimeError(
            "no group in access.json allowed_chats — add one to run group e2e tests"
        )
    return chats


def child_env(
    data_dir: Path, extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    """The SUT's environment: the operator's ``.env`` (via ``os.environ``) and
    the root ``plugins.json`` / ``access.json``, plus an isolated data dir so
    test artifacts (db, memories, renders) never touch the real ones, plus
    ``SUT_ENV_OVERRIDES`` and any per-SUT ``extra_env`` (e.g. a squeezed status
    interval for the heartbeat bot).
    """
    env = dict(os.environ)
    env.update(SUT_ENV_OVERRIDES)
    if extra_env:
        env.update(extra_env)
    env["HAMROH_DATA_DIR"] = str(data_dir)
    return env


@dataclass(frozen=True)
class E2EConfig:
    """The tester-client settings a run needs, read once from the environment.

    The SUT's own settings (bot token, model, effort) come straight from the
    root ``.env`` via ``child_env`` — they are not duplicated here.
    """

    api_id: int
    api_hash: str
    session: str
    bot_username: str
    owner_id: int

    @classmethod
    def from_env(cls) -> "E2EConfig":
        return cls(
            api_id=int(os.environ["E2E_TG_API_ID"]),
            api_hash=os.environ["E2E_TG_API_HASH"],
            session=os.environ["E2E_TG_SESSION"],
            bot_username=os.environ["E2E_BOT_USERNAME"].lstrip("@"),
            owner_id=int(os.environ["HAMROH_OWNER_ID"]),
        )
