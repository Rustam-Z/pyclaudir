"""``render_html`` + ``telegram_send_photo``: store, mocked render, mocked sender,
plus an end-to-end pipeline test gated on playwright + chromium being
installed."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hamroh.storage.render import RenderPathError, RenderStore
from hamroh.tools import render_html as render_html_mod
from hamroh.tools.base import ToolContext
from hamroh.tools.render_html import RenderHtmlArgs, RenderHtmlTool
from hamroh.tools.telegram.telegram_send_photo import (
    SendPhotoArgs,
    TelegramSendPhotoTool,
)


@pytest.fixture()
def store(tmp_path: Path) -> RenderStore:
    s = RenderStore(tmp_path / "renders")
    s.ensure_root()
    return s


# ---------------------------------------------------------------------------
# RenderStore — path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["../../../etc/passwd", "/etc/passwd", "a/../../b.png", ""],
)
def test_render_store_rejects_hostile_paths(store: RenderStore, hostile: str) -> None:
    with pytest.raises(RenderPathError):
        store.resolve_path(hostile)


def test_render_store_allocates_unique_paths(store: RenderStore) -> None:
    a = store.allocate("My Report")
    b = store.allocate("My Report")
    assert a != b
    assert a.suffix == ".png"
    assert "my-report" in a.name


def test_render_store_allocate_no_title(store: RenderStore) -> None:
    p = store.allocate(None)
    assert p.suffix == ".png"
    assert p.parent == store.root


def test_render_store_relative_roundtrip(store: RenderStore) -> None:
    p = store.allocate("x")
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header
    rel = store.relative(p)
    assert rel.endswith(".png")
    assert store.resolve_path(rel) == p.resolve()


# ---------------------------------------------------------------------------
# render_html tool — with mocked playwright
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_html_happy_path_mocked(
    store: RenderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def _fake_render(req) -> None:
        captured["html"] = req.html
        captured["width"] = req.width
        captured["height"] = req.height
        captured["out_path"] = req.out_path
        req.out_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 200)

    monkeypatch.setattr(render_html_mod, "_render_to_png", _fake_render)
    tool = RenderHtmlTool(ToolContext(render_store=store))

    result = await tool.run(
        RenderHtmlArgs(html="<table><tr><td>hi</td></tr></table>", title="report")
    )

    assert result.is_error is False
    assert captured["width"] == 800
    assert captured["height"] == 600
    assert "table" in captured["html"]
    rel = result.data["path"]
    assert rel.endswith(".png")
    assert "report" in rel
    assert (store.root / rel).exists()
    assert result.data["size_bytes"] > 0


@pytest.mark.asyncio
async def test_render_html_passes_through_dimensions(
    store: RenderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    async def _fake(req):
        seen["width"] = req.width
        seen["height"] = req.height
        req.out_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 50)

    monkeypatch.setattr(render_html_mod, "_render_to_png", _fake)
    tool = RenderHtmlTool(ToolContext(render_store=store))

    await tool.run(RenderHtmlArgs(html="<p>hi</p>", width=1200, height=900))
    assert seen == {"width": 1200, "height": 900}


@pytest.mark.asyncio
async def test_render_html_no_store_returns_error() -> None:
    tool = RenderHtmlTool(ToolContext(render_store=None))
    result = await tool.run(RenderHtmlArgs(html="<p>hi</p>"))
    assert result.is_error is True
    assert "render store" in result.content


@pytest.mark.asyncio
async def test_render_html_handles_render_failure(
    store: RenderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError("chromium crashed")

    monkeypatch.setattr(render_html_mod, "_render_to_png", _boom)
    tool = RenderHtmlTool(ToolContext(render_store=store))

    result = await tool.run(RenderHtmlArgs(html="<p>x</p>"))
    assert result.is_error is True
    assert "chromium crashed" in result.content
    # No half-written file left behind.
    assert list(store.root.glob("*.png")) == []


@pytest.mark.asyncio
async def test_render_html_handles_empty_output(
    store: RenderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _empty(req):
        req.out_path.write_bytes(b"")  # zero-byte file

    monkeypatch.setattr(render_html_mod, "_render_to_png", _empty)
    tool = RenderHtmlTool(ToolContext(render_store=store))

    result = await tool.run(RenderHtmlArgs(html="<p>x</p>"))
    assert result.is_error is True
    assert "no output" in result.content


@pytest.mark.asyncio
async def test_render_html_rejects_invalid_dimensions(store: RenderStore) -> None:
    tool = RenderHtmlTool(ToolContext(render_store=store))
    with pytest.raises(Exception):
        # Pydantic catches dimensions outside [200, 4000]
        await tool.run(RenderHtmlArgs(html="<p>x</p>", width=10))


# ---------------------------------------------------------------------------
# telegram_send_photo tool
# ---------------------------------------------------------------------------


def _mock_bot(message_id: int = 777) -> MagicMock:
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=message_id))
    bot.get_me = AsyncMock(
        return_value=MagicMock(id=1, username="bot", first_name="Bot")
    )
    return bot


@pytest.mark.asyncio
async def test_send_photo_happy_path(store: RenderStore) -> None:
    p = store.allocate("chart")
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    rel = store.relative(p)
    bot = _mock_bot(message_id=123)
    tool = TelegramSendPhotoTool(ToolContext(bot=bot, render_store=store))

    result = await tool.run(SendPhotoArgs(chat_id=42, path=rel, caption="here"))

    assert result.is_error is False
    assert "message_id=123" in result.content
    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["caption"] == "here"
    assert Path(kwargs["photo"]).read_bytes().startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_send_photo_path_traversal_rejected(store: RenderStore) -> None:
    bot = _mock_bot()
    tool = TelegramSendPhotoTool(ToolContext(bot=bot, render_store=store))
    result = await tool.run(SendPhotoArgs(chat_id=1, path="../etc/passwd"))
    assert result.is_error is True
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_photo_missing_file(store: RenderStore) -> None:
    bot = _mock_bot()
    tool = TelegramSendPhotoTool(ToolContext(bot=bot, render_store=store))
    result = await tool.run(SendPhotoArgs(chat_id=1, path="missing.png"))
    assert result.is_error is True
    assert "not found" in result.content
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_photo_no_bot(store: RenderStore) -> None:
    tool = TelegramSendPhotoTool(ToolContext(bot=None, render_store=store))
    result = await tool.run(SendPhotoArgs(chat_id=1, path="x.png"))
    assert result.is_error is True
    assert "bot not configured" in result.content


@pytest.mark.asyncio
async def test_send_photo_no_render_store() -> None:
    bot = _mock_bot()
    tool = TelegramSendPhotoTool(ToolContext(bot=bot, render_store=None))
    result = await tool.run(SendPhotoArgs(chat_id=1, path="x.png"))
    assert result.is_error is True
    assert "render store" in result.content


# ---------------------------------------------------------------------------
# Wall-clock budget — a hung launch is cancelled, not leaked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_to_png_wall_clock_enforces_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the browser launch hangs, _render_to_png raises TimeoutError on the
    wall-clock budget — proving the inner task is cancelled, not leaked."""
    pytest.importorskip("playwright.async_api")
    import hamroh.tools.render_html as m
    import playwright.async_api as pw

    # Fake async_playwright().start() whose chromium.launch hangs forever.
    class _Ch:
        async def launch(self, **_kw):
            await asyncio.sleep(60)

    class _PW:
        chromium = _Ch()

        async def stop(self) -> None:
            return None

    class _CM:  # what async_playwright() returns
        async def start(self) -> _PW:
            return _PW()

    monkeypatch.setattr(pw, "async_playwright", lambda: _CM())
    monkeypatch.setattr(m, "_WALL_CLOCK_S", 0.1)

    with pytest.raises(TimeoutError, match="wall-clock budget"):
        await m._render_to_png(
            m.RenderRequest("<p>x</p>", 800, 600, tmp_path / "x.png")
        )


# ---------------------------------------------------------------------------
# End-to-end: real chromium → real screenshot → real send (mocked bot)
# ---------------------------------------------------------------------------


def _playwright_available() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _playwright_available(),
    reason="playwright not installed; run `playwright install chromium`",
)
@pytest.mark.asyncio
async def test_render_html_end_to_end_with_real_browser(store: RenderStore) -> None:
    """Renders a real HTML table to a real PNG and routes it through
    telegram_send_photo. Skipped when playwright/chromium isn't on the host."""
    html = """
    <!DOCTYPE html>
    <html><head><style>
    body { font-family: system-ui, sans-serif; padding: 20px; }
    table { border-collapse: collapse; }
    th, td { border: 1px solid #888; padding: 8px 12px; }
    th { background: #eee; }
    </style></head>
    <body>
    <h2>Q3 Sales</h2>
    <table>
      <tr><th>Region</th><th>Revenue</th><th>YoY</th></tr>
      <tr><td>EMEA</td><td>$1.2M</td><td>+18%</td></tr>
      <tr><td>APAC</td><td>$2.4M</td><td>+9%</td></tr>
      <tr><td>Americas</td><td>$3.1M</td><td>+22%</td></tr>
    </table>
    </body></html>
    """
    tool = RenderHtmlTool(ToolContext(render_store=store))
    try:
        result = await tool.run(RenderHtmlArgs(html=html, title="q3-sales"))
    except Exception as exc:  # browser binary missing
        pytest.skip(f"chromium not usable: {exc}")

    if result.is_error and "Executable doesn't exist" in result.content:
        pytest.skip("chromium binary not installed")
    assert result.is_error is False, result.content
    rel = result.data["path"]
    assert rel.endswith(".png")
    png = (store.root / rel).read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1000  # a real screenshot, not an empty file

    # Pipe through telegram_send_photo with a mocked bot to confirm the handoff.
    bot = _mock_bot(message_id=2024)
    sender = TelegramSendPhotoTool(ToolContext(bot=bot, render_store=store))
    sent = await sender.run(SendPhotoArgs(chat_id=99, path=rel, caption="Q3"))
    assert sent.is_error is False
    bot.send_photo.assert_awaited_once()
