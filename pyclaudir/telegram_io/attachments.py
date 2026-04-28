"""Inbound-attachment download + classification.

The dispatcher calls :func:`_process_attachments` for every message that
carries a photo or document. We classify the attachment by extension/mime,
reject the unsupported ones with a marker line the model can quote, and
download the rest into ``<config.attachments_dir>/<chat_id>/``. Text
attachments get the same secret-scrub the inbound text path applies, so a
pasted API key in a ``.txt`` file never lands on disk in the clear.

The output is a list of marker strings. Each marker is one self-contained
line the dispatcher concatenates onto the message body before persistence,
so the model sees ``[attachment: /abs/path … filename=foo.jpg]`` and can
either Read the file (image/pdf/text) or apologise for the rejection.
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Message

from ..config import Config
from ..secrets_scrubber import scrub

log = logging.getLogger("pyclaudir.telegram_io")

#: Image extensions Read can render natively.
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
#: Text-like extensions safe to read as plain text. Scrubbed before saving.
_TEXT_EXTS = {
    "md", "txt", "log", "csv", "json", "yaml", "yml", "toml",
    "ini", "conf", "py", "js", "ts", "tsx", "jsx", "html", "css",
    "sh", "sql", "xml", "rst",
}


def _ext_of(name: str | None) -> str:
    if not name:
        return ""
    _, _, ext = name.rpartition(".")
    return ext.lower() if ext and ext != name else ""


def _safe_filename(name: str | None, fallback: str) -> str:
    """Strip path separators and clamp length. Falls back when name is empty."""
    if not name:
        return fallback
    cleaned = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
    cleaned = cleaned.strip(". ")
    if not cleaned:
        return fallback
    if len(cleaned) > 120:
        ext = _ext_of(cleaned)
        head = cleaned[: 120 - (len(ext) + 1 if ext else 0)]
        cleaned = f"{head}.{ext}" if ext else head
    return cleaned


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    return f"{n / (1024 * 1024 * 1024):.1f}GB"


def _classify_attachment(ext: str, mime: str | None) -> str | None:
    """Return ``"image"``, ``"pdf"``, ``"text"`` or ``None`` (rejected)."""
    if ext in _IMAGE_EXTS or (mime and mime.startswith("image/") and ext in _IMAGE_EXTS):
        return "image"
    if ext == "pdf" or mime == "application/pdf":
        return "pdf"
    if ext in _TEXT_EXTS:
        return "text"
    return None


async def _process_attachments(
    bot,
    msg: Message,
    config: Config,
) -> list[str]:
    """Download (or reject) every attachment on ``msg``, return marker lines.

    Markers point at absolute paths so the model can hand them straight to
    Read. Rejection markers explain why so the model can apologise to the
    user. Errors during download produce a third marker shape so we never
    silently lose attachments.
    """
    markers: list[str] = []
    descriptors: list[tuple[str, str, str | None, int | None]] = []
    # (file_id, filename, mime, size). Filename is synthesized for photos.

    if msg.photo:
        # Photos arrive as a list of resolutions; pick the largest.
        largest = msg.photo[-1]
        descriptors.append(
            (
                largest.file_id,
                f"photo_{msg.message_id}.jpg",
                "image/jpeg",
                largest.file_size,
            )
        )
    if msg.document is not None:
        doc = msg.document
        descriptors.append(
            (
                doc.file_id,
                doc.file_name or f"document_{msg.message_id}",
                doc.mime_type,
                doc.file_size,
            )
        )

    if not descriptors:
        return markers

    chat_dir: Path = config.attachments_dir / str(msg.chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)

    for file_id, filename, mime, size in descriptors:
        ext = _ext_of(filename)
        kind = _classify_attachment(ext, mime)
        if kind is None:
            markers.append(
                f"[attachment rejected: filename={filename} reason=unsupported_type]"
            )
            log.info(
                "attachment rejected chat=%s msg=%s filename=%s mime=%s reason=unsupported_type",
                msg.chat_id, msg.message_id, filename, mime,
            )
            continue
        if size is not None and size > config.attachment_max_bytes:
            markers.append(
                f"[attachment rejected: filename={filename} reason=too_large size={_human_size(size)}]"
            )
            log.info(
                "attachment rejected chat=%s msg=%s filename=%s size=%d reason=too_large",
                msg.chat_id, msg.message_id, filename, size,
            )
            continue

        safe_name = _safe_filename(filename, fallback=f"file_{msg.message_id}")
        dest = chat_dir / f"{msg.message_id}_{safe_name}"
        try:
            tg_file = await bot.get_file(file_id)
            await tg_file.download_to_drive(dest)
        except Exception as exc:
            markers.append(
                f"[attachment download failed: filename={filename} reason={type(exc).__name__}]"
            )
            log.warning(
                "attachment download failed chat=%s msg=%s filename=%s err=%s",
                msg.chat_id, msg.message_id, filename, exc,
            )
            continue

        if kind == "text":
            # Mirror the inbound-text scrub at telegram_io.py:62 — secrets in
            # files must not survive on disk where Read could surface them.
            try:
                raw = dest.read_text(encoding="utf-8", errors="replace")
                cleaned = scrub(raw)
                if cleaned != raw:
                    dest.write_text(cleaned, encoding="utf-8")
            except Exception as exc:  # pragma: no cover - best effort
                log.warning(
                    "attachment scrub failed path=%s err=%s", dest, exc,
                )

        actual_size = dest.stat().st_size if dest.exists() else (size or 0)
        type_str = mime or {
            "image": "image/jpeg",
            "pdf": "application/pdf",
            "text": "text/plain",
        }[kind]
        markers.append(
            f"[attachment: {dest} type={type_str} size={_human_size(actual_size)} filename={filename}]"
        )
        log.info(
            "attachment saved chat=%s msg=%s path=%s size=%d kind=%s",
            msg.chat_id, msg.message_id, dest, actual_size, kind,
        )

    return markers
