"""What an uploaded document IS, decided from its bytes — never its name or its header.

A phone sends whatever filename and `Content-Type` it likes, so neither is read. The
first bytes decide the format (JPEG, PNG, WebP, PDF), the slot decides which formats it
takes (`slots.py`: the card and the photo are photographs, a certificate may also be a
PDF), and then:

* **An image is decoded and written again** as a JPEG, upright (the camera's EXIF
  orientation is applied) and with NO metadata: a photo of an identity card taken
  at home carries the home's GPS position in its EXIF, and nothing of the upload's bytes
  survives into what is stored. A thumbnail is cut from the same pixels. Transparency is
  flattened onto white. The platform's caps (`training_platform.file_security`:
  `MAX_IMAGE_DIMENSION` a side, `MAX_IMAGE_PIXELS` in all) are checked from the header
  before a pixel is decoded, and so is what decoding would COST (`DOCUMENT_MAX_PIXELS`,
  `DECODE_BUDGET_BYTES`): a web machine has 512 MB, and before these caps one 170 KB
  PNG, decoded at 40 MP and copied three times, took it past 540 MB (the review of
  2026-09-24). A camera JPEG over the cap is decoded straight to half its size instead
  of being refused. The platform's image guard (`process_uploaded_image`) is not called:
  its 5 MB cap refuses ordinary phone photos of a document, and it drops the
  orientation, so a card would be stored on its side (the same reason
  `mediakit/processing.py` decodes for itself).
* **A PDF cannot be re-encoded here**, so it is checked instead: it must start with the
  `%PDF-` header and end with `%%EOF`; an encrypted one is refused (an operator could
  not open it), and so is one that names active content — JavaScript, a launch action,
  an embedded file, a form that submits itself. The names are matched after `#xx`
  decoding, so `/J#61vaScript` is caught; a name inside a compressed object stream is
  not visible to this check, which is why the file is also only ever served from a
  signed, `nosniff`, operator-only link (`storage.py`). The check reads each byte once
  (`_without_stream_bodies`): the expression it replaced was quadratic, and a 10 MB
  file of unterminated `stream` markers held the whole process for hours.
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

#: No image is decoded at more pixels than this. A baseline (camera) JPEG over it is
#: decoded straight to half — or a quarter of — its width and height by libjpeg's DCT
#: scaling (Pillow's `draft`) and stored at that size: an iPhone's 24 MP default becomes
#: 2856 x 2142, more than a card or a certificate needs to be read. A PNG, a WebP or a
#: progressive JPEG cannot be decoded smaller, so one over its cap is refused
#: (`verification_image_too_large`).
DOCUMENT_MAX_PIXELS = 16_000_000
#: The most one image may cost at its peak, estimated from the header before a pixel is
#: decoded (`_peak_bytes`). A web machine has 512 MB and Django already holds about
#: 110 MB of it; the worst accepted input of every kind measures under 135 MB
#: (`tests/test_review_verification_0924`). Before this, one 170 KB PNG took 540 MB.
DECODE_BUDGET_BYTES = 128 * 1024 * 1024
#: Pillow keeps 4 bytes a pixel for RGB as for RGBA, and after decoding the path holds at
#: most ONE working copy beside the image (a flatten, a mode conversion or a turn).
WORKING_BYTES_PER_PIXEL = 8
#: What a WebP costs a pixel beyond that: libwebp decodes into two canvases of its own and
#: hands Pillow a copy of the frame, 4 bytes a pixel each, and the decoder object keeps
#: its canvases until the image it made is let go (measured: 19 bytes a pixel in all).
WEBP_EXTRA_BYTES_PER_PIXEL = 12

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


def _too_large(max_pixels: int, *, side: bool = False) -> DocumentRefused:
    from training_platform.file_security import MAX_IMAGE_DIMENSION

    if side:
        message = (_("This photo is too large to open. The most is %(px)s pixels on a side.")
                   % {"px": MAX_IMAGE_DIMENSION})
    else:
        message = (_("This photo is too large to open. The most is %(mp)s megapixels for "
                     "this kind of file.") % {"mp": max(1, max_pixels // 1_000_000)})
    return DocumentRefused("verification_image_too_large", message,
                           max_pixels=max_pixels, max_side=MAX_IMAGE_DIMENSION)


def _progressive(img) -> bool:
    return img.format in ("JPEG", "MPO") and bool(img.info.get("progressive"))


def _coefficient_bytes(img, width: int, height: int) -> int:
    """The table a PROGRESSIVE JPEG is decoded into: every DCT coefficient of the whole
    image, 64 of 2 bytes for each 8x8 block of each component, in rows and columns
    padded to whole MCUs. libjpeg keeps it at FULL size whatever `draft` reduces the
    output to, because every scan refines every block: 3 bytes a pixel for ordinary
    4:2:0 colour, 6 for unsubsampled colour, 8 for CMYK. The allocator does not hand it
    back before the working copy is made, so it counts for the whole path."""
    layers = list(getattr(img, "layer", None) or [])
    if not layers:
        return width * height * 8                    # unknown: the worst there is
    across_max = max(layer[1] for layer in layers) or 1
    down_max = max(layer[2] for layer in layers) or 1
    total = 0
    for _component, across, down, _table in layers:
        # ceil(ceil(side * sampling / max sampling) / 8) blocks, then whole MCUs.
        blocks_across = -(-(-(-width * across // across_max)) // 8)
        blocks_down = -(-(-(-height * down // down_max)) // 8)
        blocks_across = -(-blocks_across // across) * across
        blocks_down = -(-blocks_down // down) * down
        total += blocks_across * blocks_down * 128
    return total


def _peak_bytes(fmt: str, size, coefficients: int) -> int:
    """What the whole path holds at its peak for an image decoded at `size`, from the
    header alone: the image and one working copy, and what its decoder adds."""
    pixels = size[0] * size[1]
    extra = pixels * WEBP_EXTRA_BYTES_PER_PIXEL if fmt == "WEBP" else coefficients
    return pixels * WORKING_BYTES_PER_PIXEL + extra


def _pixel_limit(fmt: str, coefficients: int, width: int, height: int) -> int:
    """The most pixels a file of this kind may have: what a refusal tells the trainer."""
    from training_platform.file_security import MAX_IMAGE_PIXELS

    if fmt == "JPEG" and not coefficients:
        return MAX_IMAGE_PIXELS                       # a baseline JPEG is decoded smaller
    per_pixel = WORKING_BYTES_PER_PIXEL
    if fmt == "WEBP":
        per_pixel += WEBP_EXTRA_BYTES_PER_PIXEL
    elif coefficients:                                # progressive: decoded at full size
        per_pixel += coefficients / max(1, width * height)
    return min(DOCUMENT_MAX_PIXELS, int(DECODE_BUDGET_BYTES / per_pixel))


def _draft_factor(width: int, height: int) -> int:
    """1, 2, 4 or 8: the least reduction that brings a JPEG within the cap."""
    for factor in (1, 2, 4, 8):
        if -(-width // factor) * -(-height // factor) <= DOCUMENT_MAX_PIXELS:
            return factor
    return 8


#: EXIF orientation -> the turn that makes the picture upright (as `ImageOps.exif_transpose`).
_UPRIGHT = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
            6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def _image(data: bytes, digest: str) -> ProcessedDocument:
    """Decode, turn upright, flatten and re-encode, never decoding more than the caps
    allow and never holding more than the image and ONE working copy of it.

    Each step lets go of the image it replaces (`img` is rebound; nothing else refers to
    the old one). The orientation is read from the EXIF and applied with one `transpose`
    (`exif_transpose` copies even when nothing turns); a mode is converted only when it
    is not RGB already (`convert` to the same mode is a copy); and the thumbnail is cut
    from the image itself once the full-size JPEG is written, not from a full-size
    `copy()`. Before this, the same PNG was held four times over at full size.
    """
    from PIL import ExifTags, Image

    from training_platform.file_security import MAX_IMAGE_DIMENSION

    try:
        # Opening reads the header only; nothing is decoded before the checks below.
        img = Image.open(io.BytesIO(data))
        width, height = img.size
        fmt = (img.format or "").upper()
    except Exception:
        raise _unreadable()
    if fmt == "MPO":   # a JPEG carrying a phone's depth or gain map; its first frame is kept
        fmt = "JPEG"
    if fmt not in {"JPEG", "PNG", "WEBP"}:
        raise _unreadable()

    coefficients = _coefficient_bytes(img, width, height) if _progressive(img) else 0
    limit = _pixel_limit(fmt, coefficients, width, height)
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise _too_large(limit, side=True)
    if width * height > limit:
        raise _too_large(limit)
    if min(width, height) < MIN_IMAGE_SIDE:
        raise DocumentRefused(
            "verification_image_too_small",
            _("This photo is too small to read. It needs at least %(px)s pixels on each side.")
            % {"px": MIN_IMAGE_SIDE}, min_side=MIN_IMAGE_SIDE)

    if fmt == "JPEG":
        factor = _draft_factor(width, height)
        if factor > 1:
            img.draft(None, (width // factor, height // factor))
    # What will actually be decoded: `draft` may have declined (then nothing shrank).
    decoded = img.size
    if decoded[0] * decoded[1] > DOCUMENT_MAX_PIXELS \
            or _peak_bytes(fmt, decoded, coefficients) > DECODE_BUDGET_BYTES:
        raise _too_large(limit)

    try:
        img.load()
        orientation = img.getexif().get(ExifTags.Base.Orientation, 1)
        if img.mode in ("RGBA", "LA", "PA") \
                or (img.mode == "P" and "transparency" in img.info):
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            flat = Image.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, mask=img)          # the alpha band is the mask
            img, flat = flat, None
        elif img.mode != "RGB":
            img = img.convert("RGB")
        turn = _UPRIGHT.get(orientation)
        if turn is not None:
            img = img.transpose(getattr(Image.Transpose, turn))
    except Exception:
        raise _unreadable()
    # The encoder re-emits what `info` holds (EXIF, GPS included): none of it is kept.
    img.info = {}

    width, height = img.size
    # Not `optimize`d: libjpeg would buffer every coefficient of the image to build its
    # Huffman tables (3 bytes a pixel, 48 MB at the cap) to save a few percent of a file
    # nobody but an operator downloads.
    content = _jpeg(img, quality=90, optimize=False)
    img.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.LANCZOS)
    img.info = {}
    return ProcessedDocument(kind=IMAGE, content=content, extension="jpg",
                             content_type="image/jpeg", thumbnail=_jpeg(img, quality=80),
                             sha256=digest, size=len(content), width=width, height=height)


def _jpeg(img, *, quality: int, optimize: bool = True) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=optimize)
    return out.getvalue()


_PDF_NAME = re.compile(rb"/([A-Za-z0-9#_.+\-]{2,40})")
_HEX_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
#: Where a stream's body starts: the keyword and one end of line (CRLF or LF).
_STREAM_START = re.compile(rb"stream\r?\n")


def _without_stream_bodies(data: bytes) -> bytes:
    """`data` with every stream's body taken out, each byte looked at once.

    A stream's body is data (a page's drawing, an image, a font), not structure. Read as
    names, a few megabytes of compressed bytes would sooner or later spell `/JS` by
    chance and refuse an honest certificate, so each `stream<EOL>...endstream` becomes
    `stream endstream` before the names are read.

    The expression this replaced, `stream\\r?\\n.*?endstream`, scanned to the end of the
    data from every `stream` with no `endstream` after it: quadratic, in one C call that
    holds the GIL. A 10 MB upload of `stream\\n` repeated would have held the whole web
    process for about twelve hours (the review of 2026-09-24). Here `endstream` is
    looked for once per stream, from where its body starts; when there is none, no later
    stream can have one either, so the rest is kept as it is and the scan stops. The
    output is the expression's, byte for byte.
    """
    kept, keep_from, at = [], 0, 0
    while True:
        start = _STREAM_START.search(data, at)
        if start is None:
            break
        end = data.find(b"endstream", start.end())
        if end < 0:
            break
        kept.append(data[keep_from:start.start()])
        kept.append(b"stream endstream")
        keep_from = at = end + len(b"endstream")
    kept.append(data[keep_from:])
    return b"".join(kept)


def _pdf_names(data: bytes) -> set:
    """Every name the structure uses, `#xx` escapes decoded, each distinct one once."""
    names = {match.group(1) for match in _PDF_NAME.finditer(_without_stream_bodies(data))}
    return {_HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), name)
            if b"#" in name else name for name in names}


def _pdf(data: bytes, digest: str) -> ProcessedDocument:
    if not re.match(rb"%PDF-[12]\.\d", data[:16]) or b"%%EOF" not in data[-2048:]:
        raise _unreadable()
    names = _pdf_names(data)
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
