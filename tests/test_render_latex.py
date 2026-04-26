"""``render_latex``: HTML wrapping, allow-list wiring, defenses, and
end-to-end real-Chromium tests for four LaTeX cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.render_store import RenderStore
from pyclaudir.tools import render_html as render_html_mod
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.render_latex import (
    RenderLatexArgs,
    RenderLatexTool,
    _build_html,
)


@pytest.fixture()
def store(tmp_path: Path) -> RenderStore:
    s = RenderStore(tmp_path / "renders")
    s.ensure_root()
    return s


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------


def test_build_html_includes_latex_in_dollar_block() -> None:
    html = _build_html(r"\sum_{i=0}^{n} i^2", title=None)
    assert "$$\\sum_{i=0}^{n} i^2$$" in html
    assert "katex.min.css" in html
    assert "auto-render.min.js" in html


def test_build_html_renders_title_when_present() -> None:
    html = _build_html(r"x", title="My Equation")
    assert "<h2>My Equation</h2>" in html


def test_build_html_omits_title_block_when_none() -> None:
    html = _build_html(r"x", title=None)
    assert "<h2>" not in html


def test_build_html_escapes_html_in_title() -> None:
    html = _build_html(r"x", title="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Defense: a literal </script> in the LaTeX is rejected at the tool layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_latex_rejects_script_tag_in_input(
    store: RenderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"render": False}

    async def _fake(*_a, **_kw):
        called["render"] = True

    monkeypatch.setattr(render_html_mod, "_render_to_png", _fake)
    tool = RenderLatexTool(ToolContext(render_store=store))

    result = await tool.run(RenderLatexArgs(latex="x </SCRIPT> y"))
    assert result.is_error is True
    assert "forbidden token" in result.content
    assert called["render"] is False


# ---------------------------------------------------------------------------
# Wiring: render_latex must call _render_to_png with the CDN allow-list
# and ``wait_until="networkidle"`` (so KaTeX gets a chance to render).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_latex_passes_allow_list_and_networkidle(
    store: RenderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def _fake(html, width, height, out_path, *, allowed_hosts=None,
                    wait_until="domcontentloaded"):
        captured["html"] = html
        captured["allowed_hosts"] = allowed_hosts
        captured["wait_until"] = wait_until
        out_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 200)

    monkeypatch.setattr(render_html_mod, "_render_to_png", _fake)
    tool = RenderLatexTool(ToolContext(render_store=store))

    result = await tool.run(
        RenderLatexArgs(latex=r"E = mc^2", title="famous")
    )

    assert result.is_error is False
    assert captured["allowed_hosts"] == ("cdn.jsdelivr.net",)
    assert captured["wait_until"] == "networkidle"
    assert "$$E = mc^2$$" in captured["html"]
    rel = result.data["path"]
    assert "famous" in rel  # title baked into filename


@pytest.mark.asyncio
async def test_render_latex_no_render_store() -> None:
    tool = RenderLatexTool(ToolContext(render_store=None))
    result = await tool.run(RenderLatexArgs(latex="x"))
    assert result.is_error is True
    assert "render store" in result.content


# ---------------------------------------------------------------------------
# Render_html itself stays fully network-blocked (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_html_does_not_pass_allow_list(
    store: RenderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain render_html must NOT relax the network policy."""
    captured: dict = {}

    async def _fake(html, width, height, out_path, *, allowed_hosts=None,
                    wait_until="domcontentloaded"):
        captured["allowed_hosts"] = allowed_hosts
        captured["wait_until"] = wait_until
        out_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)

    monkeypatch.setattr(render_html_mod, "_render_to_png", _fake)
    from pyclaudir.tools.render_html import RenderHtmlArgs, RenderHtmlTool
    tool = RenderHtmlTool(ToolContext(render_store=store))
    await tool.run(RenderHtmlArgs(html="<p>x</p>"))

    assert captured["allowed_hosts"] is None
    assert captured["wait_until"] == "domcontentloaded"


# ---------------------------------------------------------------------------
# End-to-end: real Chromium fetches KaTeX from jsdelivr and renders.
# Skipped when playwright/chromium isn't on the host or network is down.
# ---------------------------------------------------------------------------


def _playwright_available() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    return True


_E2E_CASES = [
    pytest.param(r"E = mc^2", "simple-eq", id="simple_equation"),
    pytest.param(
        r"\begin{aligned} a + b &= c \\ d - e &= f \end{aligned}",
        "multiline-align",
        id="multiline_align",
    ),
    pytest.param(
        r"\int_0^{\infty} \frac{x^2}{e^x - 1} \, dx = \frac{\pi^4}{15}",
        "integral-fraction",
        id="integral_with_fractions",
    ),
    pytest.param(
        r"\alpha + \beta + \gamma + \Sigma + \Omega = \pi",
        "greek-letters",
        id="greek_letters",
    ),
]


@pytest.mark.skipif(
    not _playwright_available(),
    reason="playwright not installed; run `playwright install chromium`",
)
@pytest.mark.parametrize("latex,title", _E2E_CASES)
@pytest.mark.asyncio
async def test_render_latex_end_to_end_real_browser(
    store: RenderStore, latex: str, title: str
) -> None:
    """Real Chromium fetches KaTeX from jsdelivr and renders the math.
    Skipped when the host can't reach jsdelivr (offline CI, etc.)."""
    tool = RenderLatexTool(ToolContext(render_store=store))
    try:
        result = await tool.run(RenderLatexArgs(latex=latex, title=title))
    except Exception as exc:
        pytest.skip(f"chromium not usable: {exc}")

    if result.is_error:
        # Network-flake or offline runner — soft-skip so CI without
        # internet doesn't fail.
        if "wall-clock" in result.content or "TimeoutError" in result.content:
            pytest.skip(f"network/timeout: {result.content}")
        if "Executable doesn't exist" in result.content:
            pytest.skip("chromium binary not installed")
        pytest.fail(result.content)

    rel = result.data["path"]
    png = (store.root / rel).read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1500  # a real screenshot, not blank
