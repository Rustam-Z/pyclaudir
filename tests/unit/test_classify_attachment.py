"""Attachment classification — extension and MIME signals (given/when/then)."""

from __future__ import annotations

import pytest

from hamroh.telegram_io.attachments import _classify_attachment


@pytest.mark.parametrize("ext", ["jpg", "jpeg", "png", "webp", "gif"])
def test_known_image_extension_classifies_as_image(ext: str) -> None:
    # Given a file with a known image extension and no MIME hint
    # When it is classified
    kind = _classify_attachment(ext, None)
    # Then it is accepted as an image
    assert kind == "image", f"extension {ext!r} should classify as image"


def test_image_mime_alone_classifies_as_image() -> None:
    # Given a file with an unknown extension but an image/* MIME type
    kind = _classify_attachment("heic", "image/heic")
    assert kind == "image", (
        "image/* MIME should classify as image even with unknown extension"
    )


def test_pdf_by_extension_or_mime() -> None:
    assert _classify_attachment("pdf", None) == "pdf", (
        "pdf extension should classify as pdf"
    )
    assert _classify_attachment("bin", "application/pdf") == "pdf", (
        "application/pdf MIME should classify as pdf even with unknown extension"
    )


def test_text_extension_classifies_as_text() -> None:
    assert _classify_attachment("md", None) == "text", (
        "md extension should classify as text"
    )


def test_unknown_extension_and_mime_rejected() -> None:
    # Given a file with neither a known extension nor a recognised MIME type
    kind = _classify_attachment("exe", "application/octet-stream")
    assert kind is None, "unknown extension + non-image MIME should be rejected"
