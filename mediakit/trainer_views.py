"""A coach's own demonstrations: `/api/v1/trainer/exercise-media/`.

The exercise row stays shared — one id, one history, one set of records for everybody —
and the coach's footage hangs beside it as a `trainer_exercise` asset. Forking the
exercise instead is what this replaces, and it split every client's progress in two.

Who sees whose is not decided here. It is decided once, in
`mediakit.resolution.resolve_exercise_media`, for every surface at once.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MediaAsset
from .services import payload
from .slots import IMAGE, VIDEO, OwnerType, resolve, slot_accepts
from .views import _visible_exercise


def _cap() -> int:
    return int(getattr(settings, "MEDIA_TRAINER_EXERCISE_MAX_ASSETS", 3))


def _not_found():
    """The one answer for "no such row" and for "not yours to see".

    A fresh dict per call: `normalize_error_envelope` fills `detail` and `code` into
    `response.data` in place, so a shared constant would be edited by the first 404
    this process serves. Byte-identical to the exercise routes' 404 on purpose — an
    asset is exactly as discoverable as the exercise it hangs on, and no more.
    """
    return Response({"error": _("Exercise not found."), "code": "not_found"},
                    status=status.HTTP_404_NOT_FOUND)


def _acting_trainer(request):
    """Whose library this call is about, or `None` if the caller has no business here.

    A trainer acts for themselves. An admin acts for the trainer they name in
    `trainer_id` — the operator console repairing a library — and for nobody by
    default, because an admin has no library of their own.

    There is NO verification gate. `trainer_is_verified` gates one thing in this
    codebase, the subscription paywall exemption (`subscription/permissions.py`), and
    it gates no publishing anywhere: an unverified trainer already writes exercises,
    routines and templates their clients read, and already uploads to
    `trainer.*` and `exercise.*` slots. Adding a gate here alone would be a new rule
    in one corner rather than a policy, so this follows what the rest of the platform
    does and the owner is asked for the policy (BACKEND_DELTA item 7).
    """
    user = request.user
    if getattr(user, "is_admin", False) or user.is_staff:
        raw = request.data.get("trainer_id") if request.method == "POST" \
            else request.query_params.get("trainer_id")
        if raw in (None, ""):
            return None
        from users.models import CustomUser
        return CustomUser.objects.filter(pk=_int(raw), user_type="trainer").first()
    if getattr(user, "is_trainer", False):
        return user
    return None


def _int(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


class TrainerExerciseMediaView(APIView):
    """`GET`/`POST /api/v1/trainer/exercise-media/` — a coach's own demonstrations.

    A coach puts THEIR footage on a catalogue exercise, so their client watches them
    perform the move. The exercise row is shared and is never copied: the asset is
    keyed by (trainer, exercise), which is why everyone's progress stays on one id.

    **Who may call it.** A trainer, for themselves. An admin, for the trainer they
    name in `trainer_id`. Anyone else: `403 code="not_a_trainer"`. Verification is
    NOT required — see `_acting_trainer` for why, and for the owner decision behind it.

    **Which exercises.** Only ones the caller may SEE
    (`routine.permissions.visible_exercises_for`): their own plus the shared
    catalogue. Another trainer's private exercise answers `404 code="not_found"`,
    `{"error"/"detail": "Exercise not found."}` — byte-identical to an id that does
    not exist, so the route cannot be walked to learn which ids exist.

    | Method | Answers |
    |---|---|
    | `GET ?exercise_id=` | `200` `{exercise_id, trainer_id, count, limit, media}` · `400` · `403` · `404` |
    | `POST` | `201` `{id, exercise_id, trainer_id, slot, media}` · `400` · `403` · `404` |

    **GET** takes `exercise_id` (required). It answers with what THIS trainer has on
    that exercise and nothing else: not the catalogue's, not another coach's. `media`
    is the usual `{slot: payload}` (`mediakit/services.py`); an empty slot is absent,
    and a trainer with nothing on the exercise gets `{}`.

    **POST** is `multipart/form-data`: `file` (required), `exercise_id` (required),
    `slot` (required — `exercise.poster`, `exercise.thumb`, `exercise.loop` or
    `exercise.cue`), optional `alt_ar`, `alt_en`, and `trainer_id` for an admin.
    Uploading into a slot that is already filled REPLACES it; the old file is removed
    after the transaction commits.

    Image or video is decided by the SLOT, and each goes through the platform's one
    pipeline for its kind — the same code, the same content sniffing and the same caps
    as `/api/v1/content/media/video/`. There is no second validator here: the bytes
    decide what a file is, never its name or its `Content-Type`, and the caps are
    `MEDIA_VIDEO_MAX_BYTES` (64 MB) and `MEDIA_VIDEO_MAX_SECONDS` (90 s) for video and
    the platform's image guard for stills.

    **The cap.** `MEDIA_TRAINER_EXERCISE_MAX_ASSETS` (3) assets per trainer per
    exercise. Replacing a filled slot never counts against it; filling a fourth
    different slot answers `400 code="media_library_full"`. It exists because storage
    and egress are paid per byte and a library is per trainer per exercise — a
    thousand exercises times a coach is the bill.

    | status | `code` | when |
    |---|---|---|
    | `400` | `media_exercise_id_required` | no `exercise_id` |
    | `400` | `media_file_required` | POST with no `file` part |
    | `400` | `media_unknown_slot` | not one of the four `exercise.*` slots |
    | `400` | `media_library_full` | the per-exercise cap |
    | `400` | `media_not_a_video`, `media_video_too_large`, `media_video_too_long` | the video pipeline |
    | `400` | `media_wrong_ratio`, `media_too_small`, `media_unsupported_format` | the image pipeline |
    | `400` | `media_alt_too_long` | `alt_ar` or `alt_en` over 200 characters — refused before anything is stored |
    | `403` | `not_a_trainer` | the caller has no library here |
    | `404` | `not_found` | an exercise this caller may not see, or no such exercise |
    | `413` | `payload_too_large` | the body itself is over the ceiling |

    Nothing here varies by build (`X-App-Channel`): it sells nothing and moves no
    money. Media URLs are signed and expire (`MEDIA_SIGNED_URL_TTL`, an hour) — use
    what this returns, do not store it.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        trainer = _acting_trainer(request)
        if trainer is None:
            return _refuse_not_a_trainer()

        exercise_id = _int(request.query_params.get("exercise_id"))
        if exercise_id is None:
            return Response({"error": _("Name the exercise as `exercise_id`."),
                             "code": "media_exercise_id_required"},
                            status=status.HTTP_400_BAD_REQUEST)

        exercise = _visible_exercise(request, exercise_id)
        if exercise is None:
            return _not_found()

        assets = MediaAsset.objects.filter(owner_type=OwnerType.TRAINER_EXERCISE,
                                           owner_id=exercise.id, person=trainer)
        media = {a.slot: payload(a, request) for a in assets}
        return Response({"exercise_id": exercise.id, "trainer_id": trainer.id,
                         "count": len(media), "limit": _cap(), "media": media})

    def post(self, request):
        trainer = _acting_trainer(request)
        if trainer is None:
            return _refuse_not_a_trainer()

        exercise_id = _int(request.data.get("exercise_id"))
        if exercise_id is None:
            return Response({"error": _("Name the exercise as `exercise_id`."),
                             "code": "media_exercise_id_required"},
                            status=status.HTTP_400_BAD_REQUEST)

        slot_key = (request.data.get("slot") or "").strip()
        slot = resolve(slot_key)
        if not slot_accepts(slot, OwnerType.TRAINER_EXERCISE):
            # Answered before the exercise is looked up, so a wrong slot cannot be
            # used to ask whether an id exists.
            return Response({"error": _("This slot does not belong to this kind of owner."),
                             "code": "media_unknown_slot"},
                            status=status.HTTP_400_BAD_REQUEST)

        upload = request.FILES.get("file")
        if upload is None:
            return Response({"error": _("Attach the file as `file`."),
                             "code": "media_file_required"},
                            status=status.HTTP_400_BAD_REQUEST)

        exercise = _visible_exercise(request, exercise_id)
        if exercise is None:
            return _not_found()

        # The cap counts the OTHER slots this trainer already fills on this exercise:
        # re-shooting a demonstration is not a new asset, and charging it against the
        # limit would leave a coach unable to correct their own footage.
        filled = (MediaAsset.objects
                  .filter(owner_type=OwnerType.TRAINER_EXERCISE, owner_id=exercise.id,
                          person=trainer)
                  .exclude(slot=slot_key).count())
        if filled >= _cap():
            return Response(
                {"error": _("You already have %(limit)s clips on this exercise. "
                            "Replace one instead.") % {"limit": _cap()},
                 "code": "media_library_full"},
                status=status.HTTP_400_BAD_REQUEST)

        common = dict(owner_type=OwnerType.TRAINER_EXERCISE, owner_id=exercise.id,
                      slot_key=slot_key, upload=upload, uploaded_by=request.user,
                      person=trainer, alt_ar=request.data.get("alt_ar"),
                      alt_en=request.data.get("alt_en"))
        try:
            if slot.kind == VIDEO:
                from .video import store_video
                asset = store_video(**common)
            elif slot.kind == IMAGE:
                from .services import store
                asset = store(**common)
            else:
                # SVG. `slots.py` refuses it everywhere until a sanitiser exists.
                return Response({"error": _("This slot does not accept uploads yet."),
                                 "code": "media_kind_not_supported_yet"},
                                status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as refused:
            return Response({"error": refused.messages[0],
                             "code": getattr(refused, "code", None) or "invalid"},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response({"id": asset.id, "exercise_id": exercise.id,
                         "trainer_id": trainer.id, "slot": asset.slot,
                         "media": payload(asset, request)},
                        status=status.HTTP_201_CREATED)


class TrainerExerciseMediaDetailView(APIView):
    """`DELETE /api/v1/trainer/exercise-media/{id}/` — take down your own demonstration.

    Only a `trainer_exercise` asset, and only the caller's own (an admin: anyone's).
    The shared catalogue's `exercise.*` assets are NOT reachable here whoever asks —
    they are the platform's, curated in the operator console, and a coach deleting one
    would remove it from every client of every trainer. A catalogue asset, another
    coach's asset, an asset on an exercise the caller may not see and an id that does
    not exist all answer the SAME `404 code="not_found"`, byte for byte, so the route
    cannot be walked to learn what exists or who filmed what.

    | Method | Answers |
    |---|---|
    | `DELETE` | `204` gone · `401` not signed in · `403` `not_a_trainer` · `404` `not_found` |

    The stored files are removed once the transaction commits
    (`training_platform/signals.py`). Nothing here varies by build.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, asset_id):
        user = request.user
        is_admin = getattr(user, "is_admin", False) or user.is_staff
        if not (is_admin or getattr(user, "is_trainer", False)):
            return _refuse_not_a_trainer()

        # Scoped in the QUERY, not checked afterwards: `.filter(...).first()` that
        # comes back empty is the 404, so there is no branch in which a catalogue row
        # or another coach's row is ever loaded, let alone deleted.
        assets = MediaAsset.objects.filter(pk=asset_id,
                                           owner_type=OwnerType.TRAINER_EXERCISE)
        if not is_admin:
            assets = assets.filter(person=user)
        asset = assets.first()
        if asset is None:
            return _not_found()

        # The exercise's own visibility still applies: a relation can end between the
        # upload and the delete, and the answer then has to be the same 404 the
        # exercise itself would give.
        if _visible_exercise(request, asset.owner_id) is None:
            return _not_found()

        asset.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TrainerMediaKitView(APIView):
    """`GET /api/v1/trainer/media-kit/` — the signed-in trainer's OWN media kit.

    The TrainerProfileEdit meter (BACKEND_BUGS B-094): the eight `trainer.*` slots, each
    done or missing, and how complete the kit is. It answers whatever the public roster
    lists: a trainer the directory does not show (unverified address, not yet
    listed) reads their real uploads here, where the public card
    `/api/v1/trainers/{id}/` answers 404. Auth: Bearer JWT. No body, no headers;
    nothing here varies by build (`X-App-Channel`) — it sells nothing.

    200::

        {"trainer_id": 42,
         "kit_status": {"complete": false, "filled": 3, "required": 5,
                        "missing": ["trainer.story", "trainer.avatar"]},
         "filled": 4, "total": 8,
         "slots": [{"slot": "trainer.portrait", "kind": "image", "required": true,
                    "done": true, "media": {<payload>}},
                   {"slot": "trainer.loop", "kind": "video", "required": false,
                    "done": false, "media": null}, ...],
         "media": {"trainer.portrait": {<payload>}, ...}}

    `kit_status` is `mediakit.services.kit_status` — the REQUIRED slots, the rule that
    decides whether the trainer may be featured or matched first; `filled` / `total`
    count all eight slots, the design's "5 / 8". `slots` is the eight in the slot list's
    order (`mediakit/slots.py`). `media` is the public card's `media`, the same
    `{slot: payload}` (`mediakit/services.py`: images `url_1x`/`url_2x`/`blur`/...,
    videos `video_url`/`poster_url`/`state`/...), an empty slot absent; each slot's
    `media` is that payload or null. URLs are signed and expire within the hour: use
    what this returns, never store it.

    An admin reads a trainer's kit by naming `?trainer_id=` (the console), as on
    `exercise-media/`.

    | status | `code` | when |
    |---|---|---|
    | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
    | `403` | `not_a_trainer` | the caller is not a trainer (an admin who names no trainer) |
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .services import kit_status, media_for
        from .slots import SLOTS

        trainer = _acting_trainer(request)
        if trainer is None:
            return _refuse_not_a_trainer()
        media = media_for(OwnerType.TRAINER, trainer.id, request)
        kit = [slot for slot in SLOTS.values() if slot.owner_type == OwnerType.TRAINER]
        slots = [{"slot": slot.key, "kind": slot.kind, "required": slot.required,
                  "done": slot.key in media, "media": media.get(slot.key)}
                 for slot in kit]
        return Response({
            "trainer_id": trainer.id,
            "kit_status": kit_status(OwnerType.TRAINER, media.keys()),
            "filled": sum(1 for s in slots if s["done"]),
            "total": len(slots),
            "slots": slots,
            "media": media,
        })


def _refuse_not_a_trainer():
    return Response({"error": _("Only a trainer has a library here."),
                     "code": "not_a_trainer"}, status=status.HTTP_403_FORBIDDEN)
