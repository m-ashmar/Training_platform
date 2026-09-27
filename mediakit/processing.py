"""Turn one uploaded master into what the app draws.

On upload, once: validate it against its slot, apply the black-and-white grade (the
library must match whoever shot it — ember stays the only colour on screen; food is the
owner's one exception and keeps its colour, `Slot.mono`), write WebP
at 1x and 2x, a blur preview of a few hundred bytes to paint instantly, and the average
brightness the app sets its text scrim from.

Every output is re-encoded from decoded pixels, so no metadata from the upload (EXIF,
GPS, an appended payload) survives into anything that is served.

The upload is decoded here a second time, after the platform's guard (which drops the
EXIF orientation this path needs), and that decode is priced from the header first,
like the guard's (`training_platform.image_budget`, SIDE_BUGS S-210): a web machine has
512 MB, and this path used to hold the decoded image four times over at full size — a
40 MP PNG of 170 KB would have taken over 600 MB. A baseline JPEG too large for the
budget is decoded smaller (`draft`) and its master stored at that size; anything else
is refused (`image_too_many_pixels`, the guard's code). Each step lets go of the image
it replaces, so the path holds the image, at most one full-size working copy of it,
and its grey and alpha bands (`MEDIA_BYTES_PER_PIXEL`).
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from PIL import Image, ImageOps, ImageStat

from .slots import IMAGE, RATIO_TOLERANCE, Slot

#: Formats an operator may upload. GIF is not a photograph and has no place in a kit.
#: `MPO` is a JPEG: Pillow's name for one whose MPF segment carries a second image (a
#: phone's depth or gain map). Its first image is decoded like any JPEG, and every
#: output below is re-encoded from that one frame (SIDE_BUGS S-143).
ACCEPTED_FORMATS = frozenset({"JPEG", "MPO", "PNG", "WEBP"})

#: Width of the blur preview. Twenty pixels of WebP is a few hundred bytes, which is
#: what lets the app paint something the instant a card scrolls into view.
BLUR_WIDTH = 20

#: What this path holds for each decoded pixel at its peak: the image (4 bytes), one
#: full-size working copy of it (the turn upright, the conversion, the grade or a
#: resize's intermediate: 4), its grey and alpha bands (2), and the coefficients the
#: JPEG master's `optimize` keeps (3) once the rest is let go. A WebP's decoder canvases
#: and a progressive JPEG's coefficient table are added by `image_budget`. Measured by
#: `tests/test_image_guard_memory.py`.
MEDIA_BYTES_PER_PIXEL = 12


@dataclass
class ProcessedImage:
    master: bytes
    master_ext: str
    rendition_1x: bytes
    rendition_2x: bytes
    blur: str
    brightness: float
    width: int
    height: int


def _refuse(message, code, **params):
    raise ValidationError(message, code=code, params=params or None)


def process_upload(upload, slot: Slot) -> ProcessedImage:
    """Validate `upload` for `slot` and produce every stored form of it.

    Raises `ValidationError` with a stable `code`:
    `media_kind_not_supported_yet`, `media_unsupported_format`, `media_wrong_ratio`,
    `media_too_small`, `media_needs_transparency`, and whatever the platform's upload
    guard raises for a file that is not a safe image.
    """
    if slot.kind != IMAGE:
        _refuse(_("This slot does not accept uploads yet."), "media_kind_not_supported_yet")

    # A format the kit never takes is refused from the HEADER, before the guard below
    # decodes anything: the guard re-encodes an animated GIF frame by frame, and doing
    # that only to refuse the file afterwards spent a request's memory for nothing
    # (SIDE_BUGS S-079). A file Pillow cannot even open is left to the guard, which
    # answers it with its own code.
    declared = _header_format(upload)
    if declared is not None and declared not in ACCEPTED_FORMATS:
        _refuse(_("Upload a JPEG, PNG or WebP image."), "media_unsupported_format")

    # The platform's one upload guard: magic bytes, MIME, size, pixel and dimension
    # caps, a decode. Its re-encoded output is not used — it has already discarded the
    # EXIF orientation, and a phone photo would be stored on its side.
    from training_platform.file_security import process_uploaded_image
    process_uploaded_image(upload)

    # The grade decodes the image a second time and holds it, and every form made of
    # it, until the last one is encoded: one of the process's decode slots for all of
    # it (`image_budget.decode_slot`), taken AFTER the guard has given its own back —
    # never nested. None free in time: `upload_busy`, 503, retry.
    from training_platform import image_budget
    from training_platform.api_exceptions import upload_busy_from

    try:
        with image_budget.decode_slot():
            return _grade(upload, slot)
    except image_budget.DecodeBusy as busy:
        raise upload_busy_from(busy)


def _grade(upload, slot: Slot) -> ProcessedImage:
    """Decode `upload` upright and make every stored form of it (`process_upload`)."""
    upload.seek(0)
    img = Image.open(upload)
    if (img.format or "").upper() not in ACCEPTED_FORMATS:
        _refuse(_("Upload a JPEG, PNG or WebP image."), "media_unsupported_format")
    img = _decode_upright(img)

    width, height = img.size
    expected = slot.ratio[0] / slot.ratio[1]
    if abs(width / height - expected) / expected > RATIO_TOLERANCE:
        _refuse(_("This slot needs a %(w)s:%(h)s image."), "media_wrong_ratio",
                w=slot.ratio[0], h=slot.ratio[1])
    min_w, min_h = slot.min_size
    if width < min_w or height < min_h:
        _refuse(_("This slot needs an image at least %(w)s x %(h)s pixels."),
                "media_too_small", w=min_w, h=min_h)

    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info)
    if slot.alpha and not has_alpha:
        _refuse(_("This slot needs a transparent PNG."), "media_needs_transparency")

    # The grade. Alpha slots keep their transparency; nothing else carries one.
    # Brightness is measured on the grey version either way: it drives the text scrim.
    # A mode is converted only when it differs (`convert` to the same mode is a copy),
    # and each image is let go as soon as the next one is made from it.
    if slot.alpha:
        graded = img if img.mode == "RGBA" else img.convert("RGBA")
        del img
        alpha = graded.getchannel("A")
        gray = ImageOps.grayscale(graded)
        brightness = _brightness(gray, mask=alpha)
        if slot.mono:
            del graded
            graded = Image.merge("RGBA", (gray, gray, gray, alpha))
        del alpha, gray
    else:
        graded = img if img.mode == "RGB" else img.convert("RGB")
        del img
        gray = ImageOps.grayscale(graded)
        brightness = _brightness(gray)
        if slot.mono:
            del graded
            graded = gray.convert("RGB")
        del gray
    # `convert` copies `info`, and the encoders re-emit what it holds (EXIF included).
    graded.info = {}

    return ProcessedImage(
        master=_encode(graded, "PNG" if slot.alpha else "JPEG"),
        master_ext="png" if slot.alpha else "jpg",
        rendition_1x=_encode(_resize(graded, slot.width_1x), "WEBP"),
        rendition_2x=_encode(_resize(graded, slot.width_1x * 2), "WEBP"),
        blur=_blur(graded),
        brightness=brightness,
        width=width,
        height=height,
    )


def _decode_upright(img):
    """`img` (opened, not yet loaded) decoded within the budget and turned upright.
    Raises `ValidationError` (`image_too_many_pixels`) when it cannot be."""
    from training_platform import image_budget
    from training_platform.file_security import (MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS,
                                                 _too_many_pixels)

    fmt = (img.format or "").upper()
    try:
        plan = image_budget.check(img, "JPEG" if fmt == "MPO" else fmt,
                                  per_pixel=MEDIA_BYTES_PER_PIXEL,
                                  max_pixels=MAX_IMAGE_PIXELS,
                                  max_side=MAX_IMAGE_DIMENSION)
        image_budget.shrink(img, plan)
    except image_budget.OverBudget as over:
        raise _too_many_pixels(over.max_pixels, budget=True)
    img.load()
    # One `transpose` when the EXIF says to turn, and none otherwise:
    # `ImageOps.exif_transpose` copies the whole image even when nothing turns.
    return image_budget.upright(img)


def _header_format(upload):
    """The format Pillow reads from the file's header, or None if it cannot open it.

    `Image.open` is lazy: it parses the header and decodes no pixels.
    """
    upload.seek(0)
    try:
        with Image.open(upload) as probe:
            return (probe.format or "").upper() or None
    except Exception:
        return None
    finally:
        upload.seek(0)


def _resize(img, width):
    if img.width <= width:
        return img
    height = max(1, round(img.height * width / img.width))
    return img.resize((width, height), Image.LANCZOS)


def _encode(img, fmt) -> bytes:
    out = io.BytesIO()
    if fmt == "WEBP":
        img.save(out, format="WEBP", quality=82, method=4)
    elif fmt == "JPEG":
        img.save(out, format="JPEG", quality=92, optimize=True)
    else:
        img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _blur(img) -> str:
    small = _resize(img, BLUR_WIDTH)
    out = io.BytesIO()
    small.save(out, format="WEBP", quality=30)
    return "data:image/webp;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _brightness(gray, mask=None) -> float:
    """Mean luminance, 0 (black) to 1 (white), over the visible pixels only."""
    if mask is not None and mask.getbbox() is None:
        return 0.0
    mean = ImageStat.Stat(gray, mask=mask).mean[0]
    return round(mean / 255.0, 3)
