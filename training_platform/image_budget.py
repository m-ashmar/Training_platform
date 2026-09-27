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

HOW MANY AT ONCE. The budget bounds ONE decode; a web process serves many requests at a
time (fly.toml lets a machine take 15), and three large photos decoding together passed
512 MB and took every connection on the machine down with them (the production-readiness
audit, 2026-09-25). So every decode also holds a SLOT (`decode_slot`): a per-process
bounded semaphore with as many slots as the machine's memory, less what the process
needs besides (`IMAGE_DECODE_RESERVED_BYTES`), has room for decodes at the budget — two
on a 512 MB machine. A request that cannot get a slot within `IMAGE_DECODE_WAIT_SECONDS`
is not decoded: `decode_slot` raises `DecodeBusy`, and the caller answers the retryable
`upload_busy` (503, with `Retry-After`). `IMAGE_DECODE_SLOTS` overrides the count; the
production boot check refuses a count the memory cannot hold.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
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
        # Pillow stores the SOF's sampling factors unvalidated: a crafted JPEG can carry
        # 0 for one component, and dividing by it raised ZeroDivisionError — a 500 on
        # every image route instead of the decoder's own refusal (the review,
        # 2026-09-24). Read a zero as 1, as the maxima above are; libjpeg then refuses
        # the file and each caller answers its unreadable code.
        across, down = across or 1, down or 1
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
    return check_size(fmt, (width, height), per_pixel=per_pixel, max_pixels=max_pixels,
                      max_side=max_side, max_decoded=max_decoded, budget=budget,
                      coefficients=coefficient_bytes(img, width, height))


def check_size(fmt: str, size, *, per_pixel: float, max_pixels: int, max_side: int,
               max_decoded: int | None = None, budget: int = DECODE_BUDGET_BYTES,
               coefficients: int = 0) -> Plan:
    """`check`, from a size read without opening the image (`webp_header`)."""
    width, height = size
    max_decoded = max_pixels if max_decoded is None else max_decoded
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


def webp_header(head: bytes):
    """A WebP's canvas and whether it has alpha — (width, height, alpha) — read from the
    first 30 bytes of its RIFF container, or None when `head` is not a WebP. `alpha` is
    what Pillow opens the file as: RGBA when it has alpha, RGB when not.

    Read here and not through Pillow because OPENING a WebP already allocates what it
    will be decoded into: Pillow creates libwebp's animation decoder at `Image.open`,
    and it callocs its two canvases of the full size there and then. Where the
    allocator hands back memory it has used before (macOS; glibc below its 32 MB mmap
    threshold), calloc writes zeros over all of it, so a 106 KB WebP of 6320 x 6320
    became 300 MB resident before a pixel was decoded or a check had run.
    """
    if len(head) < 30 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    chunk = head[12:16]
    if chunk == b"VP8X":      # extended: flags, then the canvas, 24 bits a side, less 1
        return (int.from_bytes(head[24:27], "little") + 1,
                int.from_bytes(head[27:30], "little") + 1,
                bool(head[20] & 0x10))
    if chunk == b"VP8L" and head[20] == 0x2F:   # lossless: 14 bits a side less 1, alpha
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool(bits >> 28 & 1)
    if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":  # lossy key frame, no alpha
        return (int.from_bytes(head[26:28], "little") & 0x3FFF,
                int.from_bytes(head[28:30], "little") & 0x3FFF, False)
    return None


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


# ------------------------------------------------------------------ how many at once
#: What a process needs besides its decodes: Django itself (about 110 MB, measured) and
#: the ordinary requests it serves beside them. The decode slots share what is left.
DEFAULT_RESERVED_BYTES = 192 * 1024 * 1024

#: How long a request waits for a slot before it is answered `upload_busy`. Long enough
#: to ride out one decode finishing (they take about a second); short enough that the
#: request is not left holding a connection behind a queue.
DEFAULT_WAIT_SECONDS = 2.0

#: What the refusal tells the app to wait before trying again (`Retry-After`).
DEFAULT_RETRY_AFTER_SECONDS = 5

_CGROUP_LIMITS = ("/sys/fs/cgroup/memory.max",                      # cgroup v2
                  "/sys/fs/cgroup/memory/memory.limit_in_bytes")    # cgroup v1


class DecodeBusy(Exception):
    """Every decode slot of this process is taken, and none came free in time. Not
    user-facing: each caller answers it as `upload_busy` (`api_exceptions.UploadBusy`)."""

    def __init__(self, retry_after: int):
        super().__init__(f"no decode slot free; retry after {retry_after} s")
        self.retry_after = retry_after


def machine_memory_bytes() -> int | None:
    """The memory this process may use: its container's cgroup limit when it has one,
    else the machine's. None when neither can be read. A cgroup with no limit reports
    "max" (v2) or a number near 2**63 (v1), so the smaller of the two figures wins."""
    figures = []
    for path in _CGROUP_LIMITS:
        try:
            with open(path) as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw.isdigit():
            figures.append(int(raw))
    try:
        figures.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        pass
    figures = [f for f in figures if f > 0]
    return min(figures) if figures else None


def slots_for(memory: int | None, *, reserved: int = DEFAULT_RESERVED_BYTES,
              budget: int = DECODE_BUDGET_BYTES) -> int:
    """How many decodes at the budget fit in `memory` beside `reserved`: whole budgets
    only, and never fewer than one (a machine too small for one is refused at boot, not
    by refusing every upload). 512 MB -> 2, 1 GB -> 6, 2 GB -> 14."""
    if not memory:
        return 1
    return max(1, (int(memory) - int(reserved)) // int(budget))


def _setting(name, default):
    try:
        from django.conf import settings

        value = getattr(settings, name, None)
    except Exception:           # settings not configured: a script, not a request
        value = None
    return default if value in (None, "") else value


def configured_slots() -> int:
    """`IMAGE_DECODE_SLOTS` when set; otherwise what the memory holds (`slots_for`),
    measured from `IMAGE_DECODE_MEMORY_BYTES` when set, else from the machine."""
    explicit = _setting("IMAGE_DECODE_SLOTS", None)
    if explicit is not None:
        return max(1, int(explicit))
    memory = _setting("IMAGE_DECODE_MEMORY_BYTES", None) or machine_memory_bytes()
    return slots_for(memory, reserved=int(_setting("IMAGE_DECODE_RESERVED_BYTES",
                                                   DEFAULT_RESERVED_BYTES)))


_slots_lock = threading.Lock()
_slots: tuple[int, threading.BoundedSemaphore] | None = None


def _semaphore() -> threading.BoundedSemaphore:
    """The process's slots, made on first use — when the settings are loaded."""
    global _slots
    if _slots is None:
        with _slots_lock:
            if _slots is None:
                count = configured_slots()
                _slots = (count, threading.BoundedSemaphore(count))
    return _slots[1]


def slot_count() -> int:
    _semaphore()
    return _slots[0]


def reset_decode_slots(count: int | None = None) -> int:
    """Make the slots again, `count` of them or as configured. For tests and for a
    process whose settings changed; a decode holding a slot of the old semaphore gives
    it back there, harmlessly."""
    global _slots
    with _slots_lock:
        count = max(1, int(count)) if count else configured_slots()
        _slots = (count, threading.BoundedSemaphore(count))
    return count


@contextmanager
def decode_slot(wait: float | None = None):
    """Hold one of the process's decode slots for the decode inside the block.

    Waits up to `wait` seconds (`IMAGE_DECODE_WAIT_SECONDS`) for one to come free, then
    raises `DecodeBusy` — the image is NOT decoded. Never nest it: a path takes a slot
    around its own decode and gives it back before another decode begins (the media
    kit runs the platform guard, and then its own grade, one after the other).
    """
    if wait is None:
        wait = float(_setting("IMAGE_DECODE_WAIT_SECONDS", DEFAULT_WAIT_SECONDS))
    semaphore = _semaphore()
    if not semaphore.acquire(timeout=max(0.0, wait)):
        raise DecodeBusy(int(_setting("IMAGE_DECODE_RETRY_AFTER_SECONDS",
                                      DEFAULT_RETRY_AFTER_SECONDS)))
    try:
        yield
    finally:
        semaphore.release()
