"""``render_latex`` — render a LaTeX expression to a PNG via KaTeX.

Thin wrapper around ``render_html``: builds a minimal HTML page with
KaTeX loaded from jsdelivr, drops the LaTeX into a ``$$…$$`` block, and
hands off to ``RenderHtmlTool._run`` with a narrow ``cdn.jsdelivr.net``
allow-list. Plain ``render_html`` stays fully network-blocked — the
relaxation is internal to this tool, not an arg the agent can flip.

Use for math formulas, equations, integrals, matrices — anything
LaTeX-native. Telegram doesn't render LaTeX inline; this fills the gap.
"""

from __future__ import annotations

from html import escape as _escape

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult
from .render_html import RenderHtmlArgs, RenderHtmlTool

#: KaTeX is fetched from jsdelivr. We allow exactly this host through the
#: render route layer; everything else stays blocked.
_KATEX_CDN_HOST = "cdn.jsdelivr.net"

#: Pinned KaTeX version. Bump deliberately — KaTeX occasionally tweaks
#: rendering between minors.
_KATEX_VERSION = "0.16.9"

#: Page sized for typical math output (single equation or multi-line align).
#: ``full_page=True`` lets the screenshot grow if the formula overflows.
_WIDTH = 900
_HEIGHT = 400

def _build_html(latex: str, title: str | None) -> str:
    """Assemble the rendered HTML page.

    LaTeX inside ``$$…$$`` is parsed by KaTeX in JS after DOM load, so
    backslashes and braces are fine; we don't HTML-escape the math body.
    The surrounding title is HTML-escaped. ``</script>`` in the LaTeX
    source is rejected at the tool layer (one cheap injection vector).
    """
    title_block = f"<h2>{_escape(title)}</h2>\n" if title else ""
    return (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8">\n'
        f'<link rel="stylesheet" href="https://{_KATEX_CDN_HOST}/npm/katex@{_KATEX_VERSION}/dist/katex.min.css">\n'
        f'<script defer src="https://{_KATEX_CDN_HOST}/npm/katex@{_KATEX_VERSION}/dist/katex.min.js"></script>\n'
        f'<script defer src="https://{_KATEX_CDN_HOST}/npm/katex@{_KATEX_VERSION}/dist/contrib/auto-render.min.js"\n'
        '        onload="renderMathInElement(document.body)"></script>\n'
        "<style>\n"
        "  body{font-family:'Computer Modern',Georgia,serif;padding:40px;\n"
        "       background:#fff;color:#111;font-size:24px;line-height:1.4}\n"
        "  h2{margin:0 0 24px;font-weight:700;font-size:22px}\n"
        "  .eq{padding:8px 0}\n"
        "</style>\n"
        "</head><body>\n"
        f"{title_block}"
        f'<div class="eq">$${latex}$$</div>\n'
        "</body></html>\n"
    )


class RenderLatexArgs(BaseModel):
    latex: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "LaTeX expression to render. Don't include the surrounding "
            "$$ delimiters — the wrapper adds them. Examples: "
            "``\\sum_{i=0}^{n} i^2``, ``\\int_0^\\infty e^{-x^2} dx``, "
            "``\\begin{align} a &= b \\\\ c &= d \\end{align}``."
        ),
    )
    title: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Optional human-readable label. Rendered as an h2 above the "
            "formula and baked into the output filename."
        ),
    )


class RenderLatexTool(BaseTool):
    name = "render_latex"
    description = (
        "Render a LaTeX expression to a PNG via KaTeX. Use for math "
        "formulas, equations, integrals, matrices — anything LaTeX-native "
        "that Telegram can't display inline. Returns the relative path "
        "under data/renders/; pair with send_photo to deliver. Pass the "
        "LaTeX without surrounding $$ — the wrapper adds them."
    )
    args_model = RenderLatexArgs

    async def run(self, args: RenderLatexArgs) -> ToolResult:
        # Defense against the one HTML-injection vector inside $$...$$:
        # a literal closing script tag would terminate the auto-render
        # script element before KaTeX could consume the math.
        if "</script" in args.latex.lower():
            return ToolResult(
                content="latex contains a forbidden token (</script)",
                is_error=True,
            )

        html = _build_html(args.latex, args.title)
        delegate = RenderHtmlTool(self.ctx)
        # ``wait_until="networkidle"`` so KaTeX has actually finished
        # rendering before we screenshot. The page is otherwise fully
        # static, so this resolves quickly.
        return await delegate._run(
            RenderHtmlArgs(
                html=html,
                width=_WIDTH,
                height=_HEIGHT,
                title=args.title or "latex",
            ),
            allowed_hosts=(_KATEX_CDN_HOST,),
            wait_until="networkidle",
        )
