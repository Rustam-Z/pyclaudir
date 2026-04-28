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
from dataclasses import dataclass
from pathlib import Path

from telegram import Message

from ..config import Config
from ..secrets_scrubber import scrub

log = logging.getLogger("pyclaudir.telegram_io")

#: Fallback MIME type when the descriptor doesn't carry one. Keyed by the
#: kind returned from :func:`_classify_attachment`.
_DEFAULT_MIME: dict[str, str] = {
    "image": "image/jpeg",
    "pdf": "application/pdf",
    "text": "text/plain",
}


@dataclass(frozen=True)
class _AttachmentDescriptor:
    """One inbound attachment we might download. Photos and documents
    both reduce to this shape; ``filename`` is synthesized for photos."""

    file_id: str
    filename: str
    mime: str | None
    size: int | None

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


def _descriptors_for(msg: Message) -> list[_AttachmentDescriptor]:
    """Reduce ``msg.photo`` (largest resolution) + ``msg.document`` to
    a flat list of descriptors. Filenames are synthesised for photos."""
    descriptors: list[_AttachmentDescriptor] = []
    if msg.photo:
        # Photos arrive as a list of resolutions; pick the largest.
        largest = msg.photo[-1]
        descriptors.append(_AttachmentDescriptor(
            file_id=largest.file_id,
            filename=f"photo_{msg.message_id}.jpg",
            mime="image/jpeg",
            size=largest.file_size,
        ))
    if msg.document is not None:
        doc = msg.document
        descriptors.append(_AttachmentDescriptor(
            file_id=doc.file_id,
            filename=doc.file_name or f"document_{msg.message_id}",
            mime=doc.mime_type,
            size=doc.file_size,
        ))
    return descriptors


async def _download_to(bot, file_id: str, dest: Path) -> str | None:
    """Download ``file_id`` to ``dest``. Returns the exception type name on
    failure, ``None`` on success — caller turns that into a marker."""
    try:
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(dest)
        return None
    except Exception as exc:
        return type(exc).__name__


def _scrub_text_attachment(dest: Path) -> None:
    """Mirror the inbound-text scrub in the dispatcher — secrets in files
    must not survive on disk where Read could surface them. Best effort."""
    try:
        raw = dest.read_text(encoding="utf-8", errors="replace")
        cleaned = scrub(raw)
        if cleaned != raw:
            dest.write_text(cleaned, encoding="utf-8")
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("attachment scrub failed path=%s err=%s", dest, exc)


def _pre_download_reject(
    d: _AttachmentDescriptor, msg: Message, max_bytes: int,
) -> tuple[str | None, str | None]:
    """Classify the descriptor and apply the size cap. Returns
    ``(kind, None)`` when accepted, or ``(None, marker)`` when rejected."""
    ext = _ext_of(d.filename)
    kind = _classify_attachment(ext, d.mime)
    if kind is None:
        log.info(
            "attachment rejected chat=%s msg=%s filename=%s mime=%s reason=unsupported_type",
            msg.chat_id, msg.message_id, d.filename, d.mime,
        )
        return None, f"[attachment rejected: filename={d.filename} reason=unsupported_type]"
    if d.size is not None and d.size > max_bytes:
        log.info(
            "attachment rejected chat=%s msg=%s filename=%s size=%d reason=too_large",
            msg.chat_id, msg.message_id, d.filename, d.size,
        )
        return None, f"[attachment rejected: filename={d.filename} reason=too_large size={_human_size(d.size)}]"
    return kind, None


async def _process_one_descriptor(
    bot,
    msg: Message,
    d: _AttachmentDescriptor,
    config: Config,
) -> str:
    """Classify, size-check, download, scrub-if-text, build marker — for
    one descriptor. Returns the marker string for the caller to collect."""
    kind, reject_marker = _pre_download_reject(d, msg, config.attachment_max_bytes)
    if reject_marker is not None:
        return reject_marker

    safe_name = _safe_filename(d.filename, fallback=f"file_{msg.message_id}")
    dest = config.attachments_dir / str(msg.chat_id) / f"{msg.message_id}_{safe_name}"
    download_err = await _download_to(bot, d.file_id, dest)
    if download_err is not None:
        log.warning(
            "attachment download failed chat=%s msg=%s filename=%s err=%s",
            msg.chat_id, msg.message_id, d.filename, download_err,
        )
        return f"[attachment download failed: filename={d.filename} reason={download_err}]"

    if kind == "text":
        _scrub_text_attachment(dest)

    actual_size = dest.stat().st_size if dest.exists() else (d.size or 0)
    type_str = d.mime or _DEFAULT_MIME[kind]
    log.info(
        "attachment saved chat=%s msg=%s path=%s size=%d kind=%s",
        msg.chat_id, msg.message_id, dest, actual_size, kind,
    )
    return f"[attachment: {dest} type={type_str} size={_human_size(actual_size)} filename={d.filename}]"


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
    descriptors = _descriptors_for(msg)
    if not descriptors:
        return []
    chat_dir: Path = config.attachments_dir / str(msg.chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    return [
        await _process_one_descriptor(bot, msg, d, config)
        for d in descriptors
    ]
