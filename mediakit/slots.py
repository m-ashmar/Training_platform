"""Every picture and video the app draws fills a named slot, and this is the list.

The photo team shoots to a slot, the operator console uploads to a slot, the API serves
the slot and the app's `YgMedia(slot)` draws it. Nobody invents a size: a slot fixes
the aspect ratio, the smallest upload that can fill it, and how it is graded (black and
white, except food, which the owner keeps in colour).

The source is the owner's Frontend Build Guide, section 5 "Media Kit". Changing a slot
here is a contract change for the app — add slots, never rename one.

Two pipelines, one slot list. An IMAGE slot is filled by `services.store` (decode,
grade, renditions — `processing.py`). A VIDEO slot (`*.loop`, `*.intro`, `*.cue`) is
filled by `video.store_video`, which validates the container from the file's own bytes,
stores the original and leaves the poster and the measurements to a worker
(`tasks.py`). Sending a video slot through the image pipeline still answers
`media_kind_not_supported_yet`: that pipeline genuinely cannot take one.

`trainer.signature` (SVG) is still refused everywhere — it needs a sanitiser with an
allow-list of elements and attributes (BACKEND_DELTA.md). HLS renditions are not built
yet either; a video slot serves the original file and leaves `hls_url` empty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

IMAGE, VIDEO, SVG = "image", "video", "svg"


class OwnerType:
    TRAINER = "trainer"      # a CustomUser with user_type='trainer'
    EXERCISE = "exercise"    # routine.Exercise
    PROGRAM = "program"      # routine.Routine
    DISH = "dish"            # diet.Recipe
    APP = "app"              # the app itself: intro story, banners, empty states

    CHOICES = [(TRAINER, "Trainer"), (EXERCISE, "Exercise"), (PROGRAM, "Program"),
               (DISH, "Dish"), (APP, "App content")]
    ALL = frozenset(v for v, _label in CHOICES)


#: The id the single app-content owner is stored under.
APP_OWNER_ID = 0


@dataclass(frozen=True)
class Slot:
    key: str
    owner_type: str
    kind: str
    ratio: tuple[int, int]
    #: Width in logical pixels at 1x. The 2x rendition is twice this, and an upload
    #: narrower than the 2x width is refused so nothing is ever upscaled.
    width_1x: int = 0
    #: Keeps transparency (the cut-out sits over the trainer's giant name).
    alpha: bool = False
    #: `app.story.N`: the key is a family and N runs from 1 to `max_index`.
    max_index: int = 0
    #: Counts towards a trainer's kit being complete. A trainer cannot be featured or
    #: matched first until every required slot is filled.
    required: bool = False
    #: Graded black and white on upload. Food keeps its colour (owner, 2026-09-19).
    mono: bool = True

    @property
    def min_size(self) -> tuple[int, int]:
        width = self.width_1x * 2
        return width, round(width * self.ratio[1] / self.ratio[0])


SLOTS: dict[str, Slot] = {s.key: s for s in (
    # Trainer kit: every trainer, shot in one session.
    Slot("trainer.portrait", OwnerType.TRAINER, IMAGE, (4, 5), 400, required=True),
    Slot("trainer.wide", OwnerType.TRAINER, IMAGE, (21, 9), 720, required=True),
    Slot("trainer.cutout", OwnerType.TRAINER, IMAGE, (3, 4), 480, alpha=True, required=True),
    Slot("trainer.story", OwnerType.TRAINER, IMAGE, (9, 16), 540, required=True),
    Slot("trainer.avatar", OwnerType.TRAINER, IMAGE, (1, 1), 200, required=True),
    Slot("trainer.loop", OwnerType.TRAINER, VIDEO, (4, 5)),
    Slot("trainer.intro", OwnerType.TRAINER, VIDEO, (16, 9)),
    Slot("trainer.signature", OwnerType.TRAINER, SVG, (3, 1)),
    # Exercise kit. The per-trainer library (a client sees their own trainer perform
    # each move) keys these by trainer as well; today they belong to the exercise row.
    Slot("exercise.poster", OwnerType.EXERCISE, IMAGE, (16, 9), 640),
    Slot("exercise.thumb", OwnerType.EXERCISE, IMAGE, (4, 5), 320),
    Slot("exercise.loop", OwnerType.EXERCISE, VIDEO, (16, 9)),
    Slot("exercise.cue", OwnerType.EXERCISE, VIDEO, (16, 9)),
    # Other kits. The guide gives dishes 4:5 and 1:1 and program covers 16:9 and 4:5:
    # one master is stored at the wider ratio and the app crops the other around the
    # focal point.
    Slot("dish.top", OwnerType.DISH, IMAGE, (4, 5), 480, mono=False),
    Slot("program.cover", OwnerType.PROGRAM, IMAGE, (16, 9), 640),
    Slot("app.story", OwnerType.APP, IMAGE, (9, 16), 540, max_index=8),
    Slot("app.banner", OwnerType.APP, IMAGE, (21, 9), 720),
    Slot("app.empty", OwnerType.APP, IMAGE, (4, 5), 400, max_index=12),
)}

#: How far an upload's aspect ratio may be from its slot's. Two percent absorbs a crop
#: a few pixels off; anything more would be visibly stretched or cut by the app.
RATIO_TOLERANCE = 0.02

_INDEXED = re.compile(r"^(?P<family>[a-z]+\.[a-z]+)\.(?P<n>[1-9]\d{0,2})$")


def resolve(key: str) -> Slot | None:
    """The slot a stored key belongs to: 'app.story.3' -> the `app.story` family."""
    key = (key or "").strip()
    slot = SLOTS.get(key)
    if slot is not None:
        return None if slot.max_index else slot
    found = _INDEXED.match(key)
    if not found:
        return None
    slot = SLOTS.get(found.group("family"))
    if slot is None or not slot.max_index or int(found.group("n")) > slot.max_index:
        return None
    return slot


def slot_keys_for(owner_type: str) -> list[str]:
    """Every concrete key an owner of this type can fill, indexed families expanded."""
    keys = []
    for slot in SLOTS.values():
        if slot.owner_type != owner_type:
            continue
        if slot.max_index:
            keys.extend(f"{slot.key}.{n}" for n in range(1, slot.max_index + 1))
        else:
            keys.append(slot.key)
    return keys


def required_keys(owner_type: str) -> list[str]:
    return [s.key for s in SLOTS.values() if s.owner_type == owner_type and s.required]
