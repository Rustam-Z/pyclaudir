"""File-backed read-only store for inbound Telegram attachments.

Scoped strictly to ``data/attachments/``. Path resolution mirrors
:class:`pyclaudir.storage.memory.MemoryStore` — same traversal-hardened rules
(no ``..``, no absolute paths outside the root, no symlinks). The dispatcher
in :mod:`pyclaudir.telegram_io` is the *only* writer; the model only ever
reads through :class:`AttachmentStore`.

Returned content depends on the file kind:

- text-like (md/txt/log/csv/json/yaml/...): UTF-8 string, capped.
- image (jpg/png/webp/gif): raw bytes + mime, for the caller to wrap as
  an MCP image content block.
- pdf: extracted text per page via ``pypdf``, joined with page markers,
  capped at the same byte budget as text-like files.

Anything else is rejected at the kind-detection step so an attacker who
manages to write into the attachments directory by hand can't trick the
model into reading an arbitrary binary blob through this surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Maximum bytes returned for a text-like attachment in one read. Larger
#: files are truncated and the truncation is marked in the returned string
#: so the model sees what happened. Mirrors ``MemoryStore``'s 64 KiB cap.
MAX_TEXT_BYTES = 64 * 1024

_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
_TEXT_EXTS = {
    "md", "txt", "log", "csv", "json", "yaml", "yml", "toml",
    "ini", "conf", "py", "js", "ts", "tsx", "jsx", "html", "css",
    "sh", "sql", "xml", "rst",
}
_IMAGE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


class AttachmentPathError(ValueError):
    """Raised when an attachment path is rejected by safety checks."""


@dataclass(frozen=True)
class TextAttachment:
    text: str
    truncated: bool
    size_bytes: int


@dataclass(frozen=True)
class ImageAttachment:
    path: Path
    mime: str
    size_bytes: int


class AttachmentStore:
    """Read-only accessor for files under ``data/attachments/``."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve_path(self, relative: str) -> Path:
        """Same hardened resolution as :class:`MemoryStore.resolve_path`.

        Rejects empty / absolute paths, ``..`` components, symlinks anywhere
        on the path, and anything that ultimately escapes the attachments
        root.
        """
        if relative is None or relative == "":
            raise AttachmentPathError("attachment path must be a non-empty string")
        if os.path.isabs(relative):
            raise AttachmentPathError(
                f"attachment path must be relative to {self._root}, got {relative!r}"
            )
        parts = Path(relative).parts
        if any(p == ".." for p in parts):
            raise AttachmentPathError(
                f"attachment path may not contain '..': {relative!r}"
            )
        check = self._root
        for part in parts:
            check = check / part
            try:
                if check.is_symlink():
                    raise AttachmentPathError(f"symlink in attachment path: {check}")
            except OSError as exc:
                raise AttachmentPathError(f"could not stat {check}: {exc}") from exc
        candidate = self._root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise AttachmentPathError(f"could not resolve {candidate}: {exc}") from exc
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise AttachmentPathError(
                f"resolved attachment path escapes root: {resolved} not under {self._root}"
            ) from exc
        return resolved

    @staticmethod
    def _ext(path: Path) -> str:
        return path.suffix.lstrip(".").lower()

    def kind(self, path: Path) -> str:
        """Classify a resolved file as ``image`` / ``text`` / ``pdf`` / ``unsupported``."""
        ext = self._ext(path)
        if ext in _IMAGE_EXTS:
            return "image"
        if ext in _TEXT_EXTS:
            return "text"
        if ext == "pdf":
            return "pdf"
        return "unsupported"

    def read_text(self, relative: str, max_bytes: int = MAX_TEXT_BYTES) -> TextAttachment:
        """Read a text-like attachment as UTF-8.

        Files larger than ``max_bytes`` are truncated; the caller may surface
        the ``truncated`` flag to the model.
        """
        path = self.resolve_path(relative)
        if not path.exists() or not path.is_file():
            raise AttachmentPathError(f"attachment not found: {relative}")
        kind = self.kind(path)
        if kind != "text":
            raise AttachmentPathError(
                f"attachment {relative} is kind={kind}; use read_image / read_pdf-equivalent flow"
            )
        size = path.stat().st_size
        raw = path.read_bytes()
        truncated = False
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
            truncated = True
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += f"\n\n[truncated to {max_bytes} bytes of {size} total]"
        return TextAttachment(text=text, truncated=truncated, size_bytes=size)

    def read_pdf(self, relative: str, max_bytes: int = MAX_TEXT_BYTES) -> TextAttachment:
        """Extract text from a PDF attachment via ``pypdf``.

        Pages are joined with ``--- page N ---`` markers so the model can
        cite specifics. Output is truncated to ``max_bytes`` bytes (UTF-8)
        for the same reason as ``read_text``: we don't want a 200-page PDF
        burning tokens silently.

        Encrypted PDFs whose password we don't have raise
        :class:`AttachmentPathError` so the model can apologise to the
        user. Image-only PDFs (scans without an OCR layer) extract to
        empty strings page by page; the marker still surfaces so the
        model can tell what happened.
        """
        from pypdf import PdfReader  # local import — keeps boot light
        from pypdf.errors import PdfReadError

        path = self.resolve_path(relative)
        if not path.exists() or not path.is_file():
            raise AttachmentPathError(f"attachment not found: {relative}")
        kind = self.kind(path)
        if kind != "pdf":
            raise AttachmentPathError(
                f"attachment {relative} is kind={kind}, not a pdf"
            )
        size = path.stat().st_size
        try:
            reader = PdfReader(str(path))
        except PdfReadError as exc:
            raise AttachmentPathError(f"could not parse PDF {relative}: {exc}") from exc
        if reader.is_encrypted:
            # pypdf returns 0 on failed decrypt and >0 on success. We try the
            # empty password (some PDFs are "encrypted" but readable that way)
            # before giving up.
            try:
                if reader.decrypt("") == 0:
                    raise AttachmentPathError(
                        f"PDF {relative} is password-protected; cannot read"
                    )
            except Exception as exc:
                raise AttachmentPathError(
                    f"PDF {relative} is password-protected; cannot read"
                ) from exc
        chunks: list[str] = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:  # pragma: no cover - pypdf can raise on weird PDFs
                page_text = ""
            chunks.append(f"--- page {i} ---\n{page_text.strip()}")
        joined = "\n\n".join(chunks)
        encoded = joined.encode("utf-8")
        truncated = False
        if len(encoded) > max_bytes:
            joined = encoded[:max_bytes].decode("utf-8", errors="ignore")
            joined += f"\n\n[truncated to {max_bytes} bytes; PDF has {len(reader.pages)} pages total]"
            truncated = True
        return TextAttachment(text=joined, truncated=truncated, size_bytes=size)

    def open_image(self, relative: str) -> ImageAttachment:
        """Resolve and validate an image attachment for binary delivery.

        Returns the absolute path + mime so the caller can hand it to the
        FastMCP ``Image`` helper. Does not read the bytes here — FastMCP
        does that lazily when serialising the content block.
        """
        path = self.resolve_path(relative)
        if not path.exists() or not path.is_file():
            raise AttachmentPathError(f"attachment not found: {relative}")
        kind = self.kind(path)
        if kind != "image":
            raise AttachmentPathError(
                f"attachment {relative} is kind={kind}, not an image"
            )
        ext = self._ext(path)
        return ImageAttachment(
            path=path,
            mime=_IMAGE_MIME[ext],
            size_bytes=path.stat().st_size,
        )
