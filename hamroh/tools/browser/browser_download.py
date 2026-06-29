"""``browser_download`` — fetch a file (usually an image) and save it to send.

Unlike ``browser_screenshot`` (which re-renders an element), this downloads the
ORIGINAL bytes — pass an image's ``src`` (from ``browser_get_attribute``) to get
the real file. Fetches through the browser's request context (carries the page's
cookies), saves under ``data/renders/``, and returns it for ``telegram_send_photo``.
Accepts http(s) URLs (private/internal hosts blocked) and inline ``data:`` URIs.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from urllib.parse import unquote_to_bytes

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool
from .ssrf import check_navigable

log = logging.getLogger(__name__)


def _decode_data_uri(url: str) -> bytes:
    """Decode a ``data:[<mediatype>][;base64],<data>`` URI to raw bytes."""
    header, sep, data = url.partition(",")
    if not sep:
        raise ValueError("malformed data: URI (no comma)")
    if header.endswith(";base64"):
        return base64.b64decode(data)
    return unquote_to_bytes(data)


async def _fetch_bytes(page, url: str) -> bytes:
    """Return the bytes at ``url`` — decode ``data:`` inline, else HTTP GET.

    http(s) goes through the SSRF guard then the page's request context;
    a non-2xx response raises so the failure surfaces to the model.
    """
    if url.startswith("data:"):
        return _decode_data_uri(url)
    check_navigable(url)  # blocks file:// and private/internal hosts
    response = await page.request.get(url)
    if not response.ok:
        raise ValueError(f"download failed: HTTP {response.status}")
    return await response.body()


class BrowserDownloadArgs(BaseModel):
    url: str = Field(
        min_length=1,
        description="http(s) URL or data: URI of the file, e.g. an image's src.",
    )
    title: str | None = Field(
        default=None,
        max_length=80,
        description="Optional label baked into the saved filename.",
    )


class BrowserDownloadTool(BrowserSessionTool[BrowserDownloadArgs]):
    name = "browser_download"
    description = (
        "Download the original file at a URL (typically an image) through the "
        "browser session and save it under data/renders/, returning its path. "
        "Pass that path to telegram_send_photo to deliver it. Get the URL first "
        "with browser_get_attribute (e.g. an <img>'s 'src'). Accepts http(s) "
        "(private hosts blocked) and data: URIs."
    )
    args_model = BrowserDownloadArgs

    async def run(self, args: BrowserDownloadArgs) -> ToolResult:
        store = self.ctx.render_store
        if store is None:
            return ToolResult(content="render store unavailable", is_error=True)
        page = self._require_page()
        data = await _fetch_bytes(page, args.url)
        out_path = store.allocate(args.title)
        await asyncio.to_thread(out_path.write_bytes, data)
        relative = store.relative(out_path)
        log.info(
            "browser downloaded %s → %s (%d bytes)", args.url[:60], relative, len(data)
        )
        # Return the PATH (not image_path): the MCP wrapper drops `content` when
        # image_path is set, hiding the path the agent needs for telegram_send_photo.
        return ToolResult(
            content=(
                f"downloaded to {relative} ({len(data)} bytes). "
                f"Pass this path to telegram_send_photo to deliver it."
            ),
            data={"path": relative, "size_bytes": len(data)},
        )
