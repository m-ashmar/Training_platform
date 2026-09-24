"""What an uploaded document IS, decided from its bytes — never its name or its header.

A phone sends whatever filename and `Content-Type` it likes, so neither is read. The
first bytes decide the format (JPEG, PNG, WebP, PDF), the slot decides which formats it
takes (`slots.py`: the card and the photo are photographs, a certificate may also be a
PDF), and then:

* **An image is decoded and written again** as a JPEG, upright (the camera's EXIF
  orientation is applied first) and with NO metadata: a photo of an identity card taken
  at home carries the home's GPS position in its EXIF, and nothing of the upload's bytes
  survives into what is stored. A thumbnail is cut from the same pixels. Transparency is
  flattened onto white. The pixel caps are the platform's
  (`training_platform.file_security.MAX_IMAGE_PIXELS` / `MAX_IMAGE_DIMENSION`), checked
  from the header before a pixel is decoded. The platform's image guard
  (`process_uploaded_image`) is not called: its 5 MB cap refuses ordinary phone photos
  of a document, and it drops the orientation, so a card would be stored on its side
  (the same reason `mediakit/processing.py` decodes for itself).
* **A PDF cannot be re-encoded here**, so it is checked instead: it must start with the
  `%PDF-` header and end with `%%EOF`; an encrypted one is refused (an operator could
  not open it), and so is one that names active content — JavaScript, a launch action,
  an embedded file, a form that submits itself. The names are matched after `#xx`
  decoding, so `/J#61vaScript` is caught; a name inside a compressed object stream is
  not visible to this check, which is why the file is also only ever served from a
  signed, `nosniff`, operator-only link (`storage.py`).
* HEIC (an iPhone's default) is recognised and refused with its own message: nothing
  on the server can decode it to strip its metadata, and an operator's browser cannot
  show it. The app sends JPEG (Flutter's `image_picker` converts by default).

Every refusal is a `DocumentRefused` with a stable `code` (the view's docstring lists
them). The size cap is `VERIFICATION_DOCUMENT_MAX_BYTES` (10 MB), under the 15 MB body
cap every request already has.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass

from django.conf import settings
from django.utils.translation import gettext as _

from .slots import IMAGE, PDF, DocumentSlot

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PDF_MAGIC = b"%PDF-"
#: ISO base media brands of HEIF/HEIC/AVIF stills (offset 8, after `ftyp` at offset 4).
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1",
               b"avif"}

#: The shorter side of a photographed document below which it cannot be read.
MIN_IMAGE_SIDE = 400
#: The thumbnail the app and the console draw in a list.
THUMBNAIL_SIDE = 480

#: PDF names that make a document do something when it is opened.
ACTIVE_PDF_NAMES = {b"JavaScript", b"JS", b"Launch", b"EmbeddedFile", b"EmbeddedFiles",
                    b"RichMedia", b"XFA", b"SubmitForm", b"ImportData", b"GoToE"}


class DocumentRefused(Exception):
    """An upload this slot will not take. `code` is what the app branches on."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class ProcessedDocument:
    kind: str                 # IMAGE | PDF
    content: bytes            # what is stored
    extension: str            # "jpg" | "pdf"
    content_type: str         # "image/jpeg" | "application/pdf"
    thumbnail: bytes | None   # a JPEG, for images; None for a PDF
    sha256: str               # of the upload AS RECEIVED: the same file twice is caught
    size: int                 # of what is stored
    width: int = 0
    height: int = 0


def max_bytes() -> int:
    return int(getattr(settings, "VERIFICATION_DOCUMENT_MAX_BYTES", 10 * 1024 * 1024))


def sniff(head: bytes) -> str | None:
    """`jpeg`, `png`, `webp`, `pdf`, `heif`, or None — from the first bytes only."""
    if head.startswith(JPEG_MAGIC):
        return "jpeg"
    if head.startswith(PNG_MAGIC):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS:
        return "heif"
    return None


def _accepted_message(slot: DocumentSlot) -> str:
    if PDF in slot.kinds:
        return _("Upload a photo (JPEG, PNG or WebP) or a PDF.")
    return _("Upload a photo (JPEG, PNG or WebP).")


def process(upload, slot: DocumentSlot) -> ProcessedDocument:
    """Validate `upload` for `slot` and produce what is stored. Raises `DocumentRefused`."""
    if upload is None:
        raise DocumentRefused("verification_file_required", _("Choose a file to upload."))
    limit = max_bytes()
    size = getattr(upload, "size", None)
    if size is not None and size > limit:
        raise DocumentRefused(
            "verification_file_too_large",
            _("The file is too large. The most is %(mb)s MB.") % {"mb": limit // (1024 * 1024)},
            max_bytes=limit)
    upload.seek(0)
    data = upload.read(limit + 1)
    if len(data) > limit:
        raise DocumentRefused(
            "verification_file_too_large",
            _("The file is too large. The most is %(mb)s MB.") % {"mb": limit // (1024 * 1024)},
            max_bytes=limit)
    if not data:
        raise DocumentRefused("verification_unreadable_file", _("The file is empty."))

    fmt = sniff(data[:32])
    accepted = list(slot.kinds)
    if fmt == "heif":
        raise DocumentRefused(
            "verification_unsupported_format",
            _("HEIC photos cannot be read here. Send the photo as JPEG."), accepted=accepted)
    if fmt is None:
        raise DocumentRefused("verification_unsupported_format", _accepted_message(slot),
                              accepted=accepted)
    kind = PDF if fmt == "pdf" else IMAGE
    if kind not in slot.kinds:
        raise DocumentRefused("verification_unsupported_format", _accepted_message(slot),
                              accepted=accepted)

    digest = hashlib.sha256(data).hexdigest()
    if kind == PDF:
        return _pdf(data, digest)
    return _image(data, digest)


def _unreadable():
    return DocumentRefused("verification_unreadable_file",
                           _("This file could not be read. Try another photo or file."))


def _image(data: bytes, digest: str) -> ProcessedDocument:
    from PIL import Image, ImageOps

    from training_platform.file_security import MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS

    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            fmt = (probe.format or "").upper()
    except Exception:
        raise _unreadable()
    if fmt == "MPO":   # a JPEG carrying a phone's depth or gain map; its first frame is kept
        fmt = "JPEG"
    if fmt not in {"JPEG", "PNG", "WEBP"}:
        raise _unreadable()
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION \
            or width * height > MAX_IMAGE_PIXELS:
        raise DocumentRefused(
            "verification_image_too_large",
            _("This photo is too large to open. The most is %(px)s pixels on a side.")
            % {"px": MAX_IMAGE_DIMENSION})

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)
    except Exception:
        raise _unreadable()

    width, height = img.size
    if min(width, height) < MIN_IMAGE_SIDE:
        raise DocumentRefused(
            "verification_image_too_small",
            _("This photo is too small to read. It needs at least %(px)s pixels on each side.")
            % {"px": MIN_IMAGE_SIDE}, min_side=MIN_IMAGE_SIDE)

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    else:
        img = img.convert("RGB")
    # `convert` copies `info`, and the encoder re-emits what it holds (EXIF included).
    img.info = {}

    content = _jpeg(img, quality=90)
    thumb = img.copy()
    thumb.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.LANCZOS)
    thumb.info = {}
    return ProcessedDocument(kind=IMAGE, content=content, extension="jpg",
                             content_type="image/jpeg", thumbnail=_jpeg(thumb, quality=80),
                             sha256=digest, size=len(content), width=width, height=height)


def _jpeg(img, *, quality: int) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


_PDF_NAME = re.compile(rb"/([A-Za-z0-9#_.+\-]{2,40})")
_HEX_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
#: A stream's body is data (a page's drawing, an image, a font), not structure. Read as
#: names, a few megabytes of compressed bytes would sooner or later spell `/JS` by chance
#: and refuse an honest certificate, so the bodies are left out of the scan.
_STREAM_BODY = re.compile(rb"stream\r?\n.*?endstream", re.S)


def _pdf_names(data: bytes):
    for match in _PDF_NAME.finditer(_STREAM_BODY.sub(b"stream endstream", data)):
        name = match.group(1)
        if b"#" in name:
            name = _HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), name)
        yield name


def _pdf(data: bytes, digest: str) -> ProcessedDocument:
    if not re.match(rb"%PDF-[12]\.\d", data[:16]) or b"%%EOF" not in data[-2048:]:
        raise _unreadable()
    names = set(_pdf_names(data))
    if b"Encrypt" in names:
        raise DocumentRefused(
            "verification_pdf_encrypted",
            _("This PDF is protected with a password. Upload a copy without one."))
    if names & ACTIVE_PDF_NAMES:
        raise DocumentRefused(
            "verification_pdf_active_content",
            _("This PDF contains scripts or attachments. Upload a plain copy, or a photo "
              "of the certificate."))
    return ProcessedDocument(kind=PDF, content=data, extension="pdf",
                             content_type="application/pdf", thumbnail=None,
                             sha256=digest, size=len(data))
