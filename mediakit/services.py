"""Storing a slot, and the one shape every API response uses for media.

    "media": {
      "trainer.portrait": {"url_1x": "...", "url_2x": "...", "blur": "data:image/webp;...",
                           "focal": [0.5, 0.3], "brightness": 0.21,
                           "alt_ar": "...", "alt_en": "..."}
    }

A slot with nothing in it is ABSENT from the object, never present with null URLs, so
the app's `YgMedia` either draws the slot or draws its typographic fallback.

A VIDEO slot carries a different set of keys in the same place — `video_url`,
`poster_url`, `duration_seconds`, `state` — see `payload()`.

Every URL is produced by `training_platform.media_storage.media_url()`: one function,
both backends, always signed and always expiring. Nothing here builds a URL by hand.

Lists must load media for the whole page at once (`media_map`), never per row.
"""
from __future__ import annotations

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from training_platform.media_storage import media_url

from .models import MediaAsset
from .processing import process_upload
from .slots import (APP_OWNER_ID, VIDEO, OwnerType, required_keys, resolve,
                    slot_accepts)


def payload(asset: MediaAsset, request=None) -> dict:
    """The one shape the API serves for a filled slot. Two kinds of slot, two shapes.

    A video slot answers, instead of the still-image keys:

        {"video_url": "<signed, expires>", "poster_url": "<signed>" (absent until the
         worker cuts one), "hls_url": "..." (absent until a transcoder writes one),
         "duration_seconds": 8.4 | null, "width": 1280, "height": 720,
         "state": "pending" | "ready" | "failed", "alt_ar": "", "alt_en": ""}

    `state` is what the app branches on: `pending` means it can play `video_url` now and
    the poster is still coming. Every URL comes from `media_url()`, which signs it and
    gives it an expiry — the app must use what the API just returned rather than storing
    one, because a stored URL stops working within the hour.

    The one exception is the app's own content (`app.*`): the same picture for every
    viewer, fetched before sign-in, so `MEDIA_PUBLIC_APP_CONTENT` may serve it as a
    plain cacheable URL. Media that belongs to a PERSON is never public.
    """
    public = (asset.owner_type == OwnerType.APP
              and getattr(settings, "MEDIA_PUBLIC_APP_CONTENT", False))
    if asset.kind == VIDEO:
        out = {
            "video_url": media_url(asset.original, request, public=public),
            "duration_seconds": asset.duration_seconds,
            "width": asset.width,
            "height": asset.height,
            "state": asset.processing_state,
            "alt_ar": asset.alt_ar,
            "alt_en": asset.alt_en,
        }
        # Absent, never null — the same rule an empty slot follows, so the app tests for
        # a key rather than for a key that may or may not hold null.
        if asset.poster:
            out["poster_url"] = media_url(asset.poster, request, public=public)
        if asset.hls_url:
            # Served as stored: an HLS player fetches the segments itself and cannot ask
            # us to sign them, so whatever writes this field writes a playable URL.
            out["hls_url"] = asset.hls_url
        return out

    return {
        "url_1x": media_url(asset.file_1x, request, public=public),
        "url_2x": media_url(asset.file_2x, request, public=public),
        "blur": asset.blur,
        "focal": [asset.focal_x, asset.focal_y],
        "brightness": asset.brightness,
        "alt_ar": asset.alt_ar,
        "alt_en": asset.alt_en,
    }


def media_map(owner_type: str, owner_ids, request=None) -> dict[int, dict]:
    """{owner_id: {slot: payload}} for every owner in `owner_ids`, in one query.

    Owners with nothing filled map to `{}`, so a caller can index without a default.
    """
    ids = {int(i) for i in owner_ids}
    out = {i: {} for i in ids}
    if not ids:
        return out
    for asset in MediaAsset.objects.filter(owner_type=owner_type, owner_id__in=ids):
        out[asset.owner_id][asset.slot] = payload(asset, request)
    return out


def media_for(owner_type: str, owner_id: int, request=None) -> dict:
    return media_map(owner_type, [owner_id], request)[int(owner_id)]


def app_media(request=None) -> dict:
    return media_for(OwnerType.APP, APP_OWNER_ID, request)


def kit_status(owner_type: str, filled_slots) -> dict:
    """How complete an owner's kit is: `{complete, filled, required, missing}`."""
    required = required_keys(owner_type)
    filled = set(filled_slots)
    missing = [k for k in required if k not in filled]
    return {"complete": not missing, "filled": len(required) - len(missing),
            "required": len(required), "missing": missing}


def complete_kits(owner_type: str, owner_ids) -> set[int]:
    """The owners among `owner_ids` whose required slots are all filled. One query."""
    required = set(required_keys(owner_type))
    if not required:
        return {int(i) for i in owner_ids}
    filled: dict[int, set] = {}
    for owner_id, slot in (MediaAsset.objects
                           .filter(owner_type=owner_type, owner_id__in=list(owner_ids),
                                   slot__in=required)
                           .values_list("owner_id", "slot")):
        filled.setdefault(owner_id, set()).add(slot)
    return {owner_id for owner_id, slots in filled.items() if required <= slots}


def owner_key(owner_type, owner_id, slot_key, person=None) -> dict:
    """The filter that names ONE stored slot.

    For every owner but `trainer_exercise` a slot is identified by (type, id, slot).
    That one is identified by (type, exercise, TRAINER, slot) — two owners share the
    exercise id, which is the whole point of the per-trainer library — and the
    database says the same thing in `one_trainer_asset_per_exercise_slot`. Anything
    that looks a slot up for writing goes through here, or it will find, replace or
    delete another coach's demonstration.
    """
    key = {"owner_type": owner_type, "owner_id": owner_id, "slot": slot_key}
    if owner_type == OwnerType.TRAINER_EXERCISE:
        key["person"] = person
    return key


def resolve_owner(owner_type, owner_id, slot_key, person=None):
    """Check that the slot belongs to this kind of owner and that the owner exists.

    Returns `(owner_id, person)`: the app's single owner id, and the person row when
    the owner belongs to one — the trainer themselves for `trainer`, the trainer who
    shot it for `trainer_exercise` (where `owner_id` is the exercise). Raises
    `ValidationError` with a `code`.
    """
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext as _

    slot = resolve(slot_key)
    if not slot_accepts(slot, owner_type):
        raise ValidationError(_("This slot does not belong to this kind of owner."),
                              code="media_unknown_slot")
    if owner_type == OwnerType.APP:
        return APP_OWNER_ID, None
    model = _OWNER_MODELS[owner_type]()
    filters = {"pk": owner_id}
    if owner_type == OwnerType.TRAINER:
        filters["user_type"] = "trainer"
    owner = model.objects.filter(**filters).first()
    if owner is None:
        raise ValidationError(_("Nothing of this kind has this id."), code="media_unknown_owner")
    if owner_type == OwnerType.TRAINER_EXERCISE:
        # `owner` is the exercise; the person is passed in, because which trainer this
        # belongs to is the CALLER's identity and can never be derived from the id.
        if person is None or not getattr(person, "is_trainer", False):
            raise ValidationError(_("Nothing of this kind has this id."),
                                  code="media_unknown_owner")
        return int(owner_id), person
    return int(owner_id), owner if owner_type == OwnerType.TRAINER else None


def _user_model():
    from users.models import CustomUser
    return CustomUser


def _exercise_model():
    from routine.models import Exercise
    return Exercise


def _routine_model():
    from routine.models import Routine
    return Routine


def _recipe_model():
    from diet.models import Recipe
    return Recipe


_OWNER_MODELS = {OwnerType.TRAINER: _user_model, OwnerType.EXERCISE: _exercise_model,
                 OwnerType.PROGRAM: _routine_model, OwnerType.DISH: _recipe_model,
                 # `trainer_exercise` is keyed on the EXERCISE: that is the row whose
                 # existence has to be proved. The trainer arrives as `person`.
                 OwnerType.TRAINER_EXERCISE: _exercise_model}


def apply_upload(asset: MediaAsset, processed) -> None:
    """Write a processed upload onto `asset` (files to storage, fields in memory)."""
    asset.blur = processed.blur
    asset.brightness = processed.brightness
    asset.width, asset.height = processed.width, processed.height
    asset.original.save(f"master.{processed.master_ext}",
                        ContentFile(processed.master), save=False)
    asset.file_1x.save("1x.webp", ContentFile(processed.rendition_1x), save=False)
    asset.file_2x.save("2x.webp", ContentFile(processed.rendition_2x), save=False)


def store(*, owner_type, owner_id, slot_key, upload, uploaded_by=None,
          focal=None, alt_ar=None, alt_en=None, person=None) -> MediaAsset:
    """Process `upload` and put it in the slot, replacing whatever was there.

    `person` is required for `trainer_exercise` and ignored everywhere else: it is the
    trainer whose demonstration this is, and it is half of that owner's key.

    Raises `django.core.exceptions.ValidationError` (with a `code`) when the upload or
    the slot is refused. The replaced files are removed once the transaction commits
    (`training_platform/signals.py`), so a rollback never leaves a row without its files.
    """
    owner_id, person = resolve_owner(owner_type, owner_id, slot_key, person)
    processed = process_upload(upload, resolve(slot_key))
    key = owner_key(owner_type, owner_id, slot_key, person)

    with transaction.atomic():
        asset = (MediaAsset.objects.select_for_update().filter(**key).first()
                 or MediaAsset(**key))
        asset.person = person
        asset.uploaded_by = uploaded_by
        if focal is not None:
            asset.focal_x, asset.focal_y = (min(1.0, max(0.0, float(v))) for v in focal)
        if alt_ar is not None:
            asset.alt_ar = alt_ar
        if alt_en is not None:
            asset.alt_en = alt_en
        apply_upload(asset, processed)
        asset.full_clean()
        asset.save()
    return asset
