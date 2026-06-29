"""Latency + correctness eval core, driven by ``test_eval_e2e.py``.

Runs each scenario in ``models.py`` ``runs`` times in one chat (a DM or the
group — the DM and group cases are separate tests), then builds a per-feature
table: pass rate, p50/p95 of the send->first-reply and send->complete latencies,
and the mean tool time per turn (sum of ``tool_calls.duration_ms``).

Tool time vs turn time is the speed-attribution signal: a turn that takes
seconds but whose tools take milliseconds is dominated by Claude inference plus
Telegram round-trips, not by tool I/O (e.g. memory read/write or rendering).

The caller supplies an already-running bot (the session ``hamroh_sut``) and a
connected client, so the eval reuses the one e2e bot instead of launching its
own — two bots can't share a Telegram token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.client import has_photo, send_and_wait, send_and_wait_until
from tests.e2e.support.config import MAX_RENDER_REPLY_S
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.models import SCENARIOS, Conversation, Reply, Scenario
from tests.e2e.support.state import tool_calls_since

_HEADER = (
    f"{'feature':<13}{'chat':<7}{'runs':>5}{'pass':>6}"
    f"{'p50_1st':>9}{'p95_1st':>9}{'p50_done':>10}{'tool_s':>9}"
)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    if lo + 1 >= len(s):
        return s[lo]
    return s[lo] + (s[lo + 1] - s[lo]) * (k - lo)


def _passed(reply: Reply, token: str, check: str) -> bool:
    if check == "photo":
        return reply.media_kind == "photo"
    if check == "contains":
        return token in reply.text
    return bool(reply.text or reply.media_kind)


@dataclass
class RunResult:
    ok: bool
    first: float
    complete: float
    tool_ms: int


async def _send_scenario(
    client: TelegramClient, convo: Conversation, prompt: str, check: str
) -> Reply:
    """Drive one scenario, choosing the wait that fits its success signal.

    A ``photo`` scenario (render) often sends a "working on it" text first and
    the photo only after, so we keep waiting for the message that carries the
    photo — the quiet-window wait used for text replies would stop at the
    confirmation and miss the image. Same generous wait as the render e2e test;
    the eval logs the latency rather than gating on it.
    """
    if check == "photo":
        return await send_and_wait_until(
            client, convo, prompt, until=has_photo, timeout=MAX_RENDER_REPLY_S
        )
    return await send_and_wait(client, convo, prompt, timeout=MAX_RENDER_REPLY_S)


async def _run_once(
    client: TelegramClient, convo: Conversation, db_path: Path, scenario: Scenario
) -> RunResult:
    token = new_sentinel("EVAL")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    reply = await _send_scenario(
        client, convo, scenario.prompt.format(token=token), scenario.check
    )
    tool_ms = sum(r["duration_ms"] or 0 for r in tool_calls_since(db_path, since))
    return RunResult(
        _passed(reply, token, scenario.check),
        reply.t_first_s,
        reply.t_complete_s,
        tool_ms,
    )


def _row(name: str, chat: str, results: list[RunResult]) -> str:
    n = len(results)
    passed = sum(r.ok for r in results)
    first = [r.first for r in results]
    done = [r.complete for r in results]
    tool_s = sum(r.tool_ms for r in results) / n / 1000 if n else 0.0
    return (
        f"{name:<13}{chat:<7}{n:>5}{(100 * passed // n if n else 0):>5}%"
        f"{_pct(first, 50):>9.1f}{_pct(first, 95):>9.1f}"
        f"{_pct(done, 50):>10.1f}{tool_s:>9.2f}"
    )


@dataclass(frozen=True)
class EvalReport:
    """The formatted latency table plus every run, so a caller can gate on the
    correctness pass-rate while only logging the (noisier) latency numbers."""

    table: str
    results: list[RunResult]

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.ok for r in self.results) / len(self.results)


def chat_label(convo: Conversation) -> str:
    """``"group"`` when the bot must be @mentioned, else ``"dm"``."""
    return "group" if convo.mention else "dm"


async def run_eval(
    client: TelegramClient, convo: Conversation, db_path: Path, runs: int
) -> EvalReport:
    """Run every scenario ``runs`` times in one chat and tabulate the result."""
    chat = chat_label(convo)
    lines = [_HEADER]
    results: list[RunResult] = []
    for scenario in SCENARIOS:
        rs = [await _run_once(client, convo, db_path, scenario) for _ in range(runs)]
        results.extend(rs)
        lines.append(_row(scenario.name, chat, rs))
    return EvalReport("\n".join(lines), results)
