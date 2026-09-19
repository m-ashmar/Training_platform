"""Turn one uploaded master into what the app draws.

On upload, once: validate it against its slot, apply the black-and-white grade (the
library must match whoever shot it — ember stays the only colour on screen), write WebP
at 1x and 2x, a blur preview of a few hundred bytes to paint instantly, and the average
brightness the app sets its text scrim from.

Every output is re-encoded from decoded pixels, so no metadata from the upload (EXIF,
GPS, an appended payload) survives into anything that is served.
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
ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})

#: Width of the blur preview. Twenty pixels of WebP is a few hundred bytes, which is
#: what lets the app paint something the instant a card scrolls into view.
BLUR_WIDTH = 20


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

    # The platform's one upload guard: magic bytes, MIME, size, pixel and dimension
    # caps, a decode. Its re-encoded output is not used — it has already discarded the
    # EXIF orientation, and a phone photo would be stored on its side.
    from training_platform.file_security import process_uploaded_image
    process_uploaded_image(upload)

    upload.seek(0)
    img = Image.open(upload)
    if (img.format or "").upper() not in ACCEPTED_FORMATS:
        _refuse(_("Upload a JPEG, PNG or WebP image."), "media_unsupported_format")
    img = ImageOps.exif_transpose(img)
    img.load()

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
    if slot.alpha:
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A")
        gray = ImageOps.grayscale(rgba)
        graded = Image.merge("RGBA", (gray, gray, gray, alpha))
        brightness = _brightness(gray, mask=alpha)
    else:
        gray = ImageOps.grayscale(img.convert("RGB"))
        graded = gray.convert("RGB")
        brightness = _brightness(gray)
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
