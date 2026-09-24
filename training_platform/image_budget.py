"""What decoding an uploaded image will COST, estimated from its header before a pixel is
decoded — one estimator for every path that decodes an upload.

Those paths are the platform's image guard (`file_security.process_uploaded_image`,
behind the profile picture, chat photos, exercise images, social posts and the media
kit), the media kit's own grade (`mediakit/processing.py`) and trainer verification
(`trainer_verification/files.py`).

A web machine has 512 MB (`fly.toml`) and a Django process already holds about 110 MB of
it, while a small file can decode to a very large image: through the guard, a 106 KB
WebP of 6320 x 6320 added 983 MB and a 170 KB RGBA PNG 153 MB (SIDE_BUGS S-210). So each
path says what it holds for every decoded pixel (`per_pixel`, measured on that path), and
an image whose peak (`peak_bytes`) would pass `DECODE_BUDGET_BYTES` is not decoded: a
baseline JPEG is decoded smaller instead (libjpeg's DCT scaling, Pillow's `draft`, straight
to a half, a quarter or an eighth of its width and height), and anything else is refused
with the most pixels an image of its kind may have (`OverBudget.max_pixels`).

What a decoder keeps beside the image is counted too: a WebP's two canvases and the frame
copy (`WEBP_EXTRA_BYTES_PER_PIXEL`), and a progressive JPEG's table of every coefficient,
which libjpeg holds at full size whatever `draft` asks for (`coefficient_bytes`).

Nothing here decodes anything, and nothing here is user-facing: each caller turns
`OverBudget` into its own `code` and sentence.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The most one decode may add to a web process at its peak, estimated from the header.
#: A 512 MB machine holding ~110 MB of Django keeps room for this and a second request;
#: the worst accepted input of every kind measures under 150 MB on every path
#: (`tests/test_review_verification_0924.py`, `tests/test_image_guard_memory.py`).
DECODE_BUDGET_BYTES = 128 * 1024 * 1024

#: What a WebP costs a pixel beyond the image and the path's working copies: libwebp
#: decodes into two canvases of its own and hands Pillow a copy of the frame, 4 bytes a
#: pixel each, and the decoder object keeps its canvases until the image it made is let
#: go (measured: 19 bytes a pixel for a decode and one working copy).
WEBP_EXTRA_BYTES_PER_PIXEL = 12

#: libjpeg's DCT scaling: the reductions `draft` can decode a baseline JPEG straight to.
DRAFT_FACTORS = (1, 2, 4, 8)

#: EXIF orientation -> the turn that makes the picture upright (as `ImageOps.exif_transpose`).
UPRIGHT = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
           6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def mode_bytes(mode: str) -> int:
    """What Pillow stores for one pixel of `mode`: one byte for a bilevel, grey or
    palette image, two for 16-bit grey, and four for everything else — RGB is stored four
    wide, like RGBA, LA and CMYK."""
    if mode in ("1", "L", "P"):
        return 1
    if mode.startswith("I;16"):
        return 2
    return 4


def is_progressive(img) -> bool:
    return img.format in ("JPEG", "MPO") and bool(img.info.get("progressive"))


def coefficient_bytes(img, width: int, height: int) -> int:
    """The table a PROGRESSIVE JPEG is decoded into: every DCT coefficient of the whole
    image, 64 of 2 bytes for each 8x8 block of each component, in rows and columns
    padded to whole MCUs. libjpeg keeps it at FULL size whatever `draft` reduces the
    output to, because every scan refines every block: 3 bytes a pixel for ordinary
    4:2:0 colour, 6 for unsubsampled colour, 8 for CMYK. The allocator does not hand it
    back before the working copy is made, so it counts for the whole path. 0 for any
    other image."""
    if not is_progressive(img):
        return 0
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


def peak_bytes(fmt: str, size, per_pixel: float, coefficients: int = 0) -> int:
    """What a path holds at its peak for an image decoded at `size`, from the header
    alone: `per_pixel` bytes for each pixel (the image and what the path makes of it),
    and what the decoder itself keeps."""
    pixels = size[0] * size[1]
    extra = pixels * WEBP_EXTRA_BYTES_PER_PIXEL if fmt == "WEBP" else coefficients
    return int(pixels * per_pixel + extra)


def draftable(fmt: str, coefficients: int) -> bool:
    """A baseline JPEG can be decoded smaller than it is; nothing else can."""
    return fmt == "JPEG" and not coefficients


class OverBudget(Exception):
    """This image may not be decoded here. `max_pixels` is the most pixels an image of its
    kind may have on this path; `side` is True when a side is over `max_side`."""

    def __init__(self, max_pixels: int, *, side: bool = False, max_side: int = 0):
        super().__init__(f"over the decode budget: at most {max_pixels} pixels")
        self.max_pixels = max_pixels
        self.side = side
        self.max_side = max_side


@dataclass(frozen=True)
class Plan:
    """What `check` found in the header, and what `shrink` needs to hold the decode to it."""
    fmt: str
    width: int
    height: int
    per_pixel: float
    coefficients: int
    max_pixels: int          # the limit a refusal names
    max_decoded: int         # the most pixels the decode itself may produce
    budget: int

    def fits(self, size) -> bool:
        return (size[0] * size[1] <= self.max_decoded
                and peak_bytes(self.fmt, size, self.per_pixel, self.coefficients)
                <= self.budget)


def check(img, fmt: str, *, per_pixel: float, max_pixels: int, max_side: int,
          max_decoded: int | None = None, budget: int = DECODE_BUDGET_BYTES) -> Plan:
    """The header's verdict, before anything is decoded. Raises `OverBudget`.

    `max_pixels` is the most a file may have at all (the platform's `MAX_IMAGE_PIXELS`);
    a baseline JPEG may have that many, because it is decoded smaller (`shrink`).
    Anything else may have only as many as its peak allows at full size, and
    `max_decoded` if the path caps the decoded size itself (verification does).
    """
    width, height = img.size
    max_decoded = max_pixels if max_decoded is None else max_decoded
    coefficients = coefficient_bytes(img, width, height)
    if draftable(fmt, coefficients):
        limit = max_pixels
    else:
        cost = per_pixel + (WEBP_EXTRA_BYTES_PER_PIXEL if fmt == "WEBP"
                            else coefficients / max(1, width * height))
        limit = min(max_pixels, max_decoded, int(budget / cost))
    if width > max_side or height > max_side:
        raise OverBudget(limit, side=True, max_side=max_side)
    if width * height > limit:
        raise OverBudget(limit, max_side=max_side)
    return Plan(fmt=fmt, width=width, height=height, per_pixel=per_pixel,
                coefficients=coefficients, max_pixels=limit, max_decoded=max_decoded,
                budget=budget)


def draft_factor(plan: Plan) -> int:
    """1, 2, 4 or 8: the least reduction whose decode fits the plan (8 when none does)."""
    for factor in DRAFT_FACTORS:
        size = (-(-plan.width // factor), -(-plan.height // factor))
        if plan.fits(size):
            return factor
    return DRAFT_FACTORS[-1]


def shrink(img, plan: Plan):
    """Ready `img` (opened, not yet loaded) to be decoded within `plan`, and answer the
    size it WILL be decoded at. A baseline JPEG over the plan is drafted to the least
    reduction that fits; then what will actually be decoded is checked again (`draft`
    may decline, and then nothing shrank). Raises `OverBudget`."""
    if draftable(plan.fmt, plan.coefficients):
        factor = draft_factor(plan)
        if factor > 1:
            img.draft(None, (plan.width // factor, plan.height // factor))
    decoded = img.size
    if not plan.fits(decoded):
        raise OverBudget(plan.max_pixels)
    return decoded


def upright(img):
    """`img` turned the way its EXIF orientation says, read after `load()`: the same image
    when nothing turns (`ImageOps.exif_transpose` copies even then), else ONE transpose."""
    from PIL import ExifTags, Image

    turn = UPRIGHT.get(img.getexif().get(ExifTags.Base.Orientation, 1))
    if turn is None:
        return img
    return img.transpose(getattr(Image.Transpose, turn))
