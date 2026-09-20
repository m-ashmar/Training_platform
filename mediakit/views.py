from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import app_media
from .slots import VIDEO, OwnerType, resolve, slot_accepts


class AppMediaView(APIView):
    """GET /api/v1/content/media (with or without the trailing slash) — the pictures the
    app itself shows.

    Open to everyone, visitors included: the intro story plays before sign-in. Returns
    `{"media": {"app.story.1": {...}, "app.banner": {...}, "app.empty.3": {...}}}` in the
    shape documented in `mediakit/services.py`. An empty slot is absent; the app draws
    its built-in fallback for it. Identical for every caller and every build.

    Slots: `app.story.1`-`app.story.8` (9:16, intro story backgrounds), `app.banner`
    (21:9, home and seasonal banners), `app.empty.1`-`app.empty.12` (4:5, empty states).
    Operators fill them in the console (Media kit).
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"media": app_media(request)})


def _visible_exercise(request, exercise_id):
    """The exercise if this caller may READ it, else None.

    `routine.permissions.visible_exercises_for` — the one rule, the same one the
    exercise list, the detail route and `add-media/` use. A coach's library is private:
    another trainer's exercise is answered exactly like an id that does not exist.
    """
    from routine.permissions import visible_exercises_for

    return visible_exercises_for(request.user).filter(id=exercise_id).first()


def _may_modify_exercise(request, exercise):
    """Writes are owner-only: `created_by`, or an admin (the shared catalogue has no
    owner, so it is admin-only). Copied in behaviour from `ExerciseAddMediaView`, which
    is the route this one sits beside."""
    is_admin = getattr(request.user, "is_admin", False) or request.user.is_staff
    if exercise.created_by_id:
        return (exercise.created_by_id == request.user.id) or is_admin
    return is_admin


class ExerciseMediaView(APIView):
    """GET /api/v1/content/media/exercise/{id}/ — one exercise's media slots.

    The coach's library: `exercise.poster` and `exercise.thumb` (stills) and
    `exercise.loop` and `exercise.cue` (video), in the shape `mediakit/services.py`
    documents. An empty slot is absent.

    **WHOSE media.** Not the catalogue's, necessarily. This goes through
    `mediakit.resolution.resolve_exercise_media`, the platform's ONE answer to that
    question — the author of the routine it was read out of, else a trainer the caller
    has an approved relation with, else the shared catalogue — so the client watches
    their own coach perform the move. The response carries `media_source`
    (`routine_author` | `my_trainer` | `catalogue`, or `null` when there is no media
    anywhere) and `media_trainer_id` (`null` for the catalogue), so the app can caption
    it. Pass `?routine_id=` when the exercise was opened from inside a routine and the
    first line of the rule should apply; a routine the caller cannot see is ignored
    rather than refused, because it changes nothing they may not already read.

    **Visibility is the exercise's own, and video obeys exactly the same rule as the
    pictures.** The id is resolved through `routine.permissions.visible_exercises_for`:
    admin sees every exercise, a trainer their own plus the shared catalogue, a client
    the catalogue, their approved trainers' exercises, and anything a routine assigned
    to them uses. Anything else answers `404 code="not_found"`,
    `{"error"/"detail": "Exercise not found."}` — byte-identical to an id that does not
    exist, so the route cannot be walked to discover which ids exist.

    | Method | Answers |
    |---|---|
    | `GET` | `200` `{exercise_id, media, media_source, media_trainer_id}` · `401` · `404` |

    Nothing here varies by build (`X-App-Channel`): it sells nothing and moves no money.
    The URLs inside are signed and expire (`MEDIA_SIGNED_URL_TTL`, default one hour) —
    play or draw what this returns, do not store it.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, exercise_id):
        from .resolution import resolve_exercise_media

        exercise = _visible_exercise(request, exercise_id)
        if exercise is None:
            return Response({"error": _("Exercise not found.")},
                            status=status.HTTP_404_NOT_FOUND)

        answer = resolve_exercise_media(
            request.user, [exercise.id], request=request,
            routine_author_id=_routine_author(request))[exercise.id]
        return Response({"exercise_id": exercise.id, **answer})


def _routine_author(request):
    """The `created_by` of `?routine_id=`, when the caller may read that routine.

    Line (a) of the rule — "the coach who prescribed it is the one demonstrating it" —
    needs to know which routine the exercise was opened from, and only the app knows.
    Scoped to routines the caller is part of so the parameter cannot be used to ask
    who wrote routine 41: an id they are not part of is simply ignored, and the answer
    is the one they would have got without the parameter.
    """
    from routine.models import Routine

    raw = request.query_params.get("routine_id")
    if not raw:
        return None
    try:
        routine_id = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    user = request.user
    mine = Routine.objects.filter(pk=routine_id)
    if not (getattr(user, "is_admin", False) or user.is_staff):
        from django.db.models import Q
        mine = mine.filter(Q(assigned_to=user) | Q(created_by=user))
    return mine.values_list("created_by_id", flat=True).first()


class VideoUploadView(APIView):
    """POST /api/v1/content/media/video/ — put a video in a slot. `multipart/form-data`.

    The coach's library is images AND video; this is how the video gets in.

    **Body** (multipart): `file` (required, the video), `slot` (required, e.g.
    `exercise.loop`), `owner_type` (required: `trainer`, `exercise`, `program`, `dish`,
    `app`), `owner_id` (the exercise/program/dish id, or the trainer's user id; ignored
    for `app`), optional `alt_ar` and `alt_en`.

    **Who may upload what**

    | owner_type | who |
    |---|---|
    | `exercise` | the trainer who created it, or an admin |
    | `trainer` | that trainer themselves, or an admin |
    | `program`, `dish`, `app` | admin only |

    An exercise the caller cannot SEE answers 404, exactly as
    `/api/routine/exercises/{id}/add-media/` does — byte-identical to a missing id, so a
    403 never confirms that someone else's exercise exists. One they can see but do not
    own answers 403.

    **The bytes decide.** The container is sniffed from the file itself
    (`training_platform/file_security.py`): an `.mp4` that is really a script, an image
    or an HTML page is refused however it is named and whatever `Content-Type` the
    client sends. Only MP4/MOV and WebM are accepted. The size cap is
    `MEDIA_VIDEO_MAX_BYTES` (64 MB) and the duration cap `MEDIA_VIDEO_MAX_SECONDS`
    (90 s) — the request body itself is capped a little above the first of those, and a
    bigger body is refused by the middleware with `413 code="payload_too_large"`.

    **Answers**

    | status | `code` | when |
    |---|---|---|
    | `201` | — | `{"slot": "...", "media": {...}}` — the slot's payload, `state: "pending"` |
    | `400` | `media_file_required` | no `file` part |
    | `400` | `media_unknown_slot` | no such slot, or not one this kind of owner has |
    | `400` | `media_unknown_owner` | nothing of that kind has that id |
    | `400` | `media_slot_is_not_video` | that slot holds a still image or an SVG |
    | `400` | `media_not_a_video` | the bytes are not an MP4 or a WebM |
    | `400` | `media_video_too_large` | over the size cap |
    | `400` | `media_video_too_long` | over the duration cap |
    | `403` | `permission_denied` | visible, but not the caller's to change |
    | `404` | `not_found` | an exercise this caller may not see |
    | `413` | `payload_too_large` | the body itself is over the ceiling |

    `state` is `pending` until a worker fills the poster and the measurements; the video
    already plays. See `mediakit/tasks.py` for what fills it and what does not (`hls_url`
    stays empty until a transcoder exists). Nothing here varies by build.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from .video import store_video

        slot_key = (request.data.get("slot") or "").strip()
        owner_type = (request.data.get("owner_type") or "").strip()
        raw_owner_id = request.data.get("owner_id") or 0
        upload = request.FILES.get("file")

        if upload is None:
            return Response({"error": _("Attach the video as `file`."),
                             "code": "media_file_required"},
                            status=status.HTTP_400_BAD_REQUEST)

        slot = resolve(slot_key)
        if (owner_type not in OwnerType.ALL
                or not slot_accepts(slot, owner_type)):
            # Answered before the owner is looked up, so a wrong slot cannot be used to
            # ask whether an id exists.
            return Response({"error": _("This slot does not belong to this kind of owner."),
                             "code": "media_unknown_slot"},
                            status=status.HTTP_400_BAD_REQUEST)
        if slot.kind != VIDEO:
            return Response({"error": _("This slot does not take a video."),
                             "code": "media_slot_is_not_video"},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            owner_id = int(raw_owner_id)
        except (TypeError, ValueError):
            return Response({"error": _("Nothing of this kind has this id."),
                             "code": "media_unknown_owner"},
                            status=status.HTTP_400_BAD_REQUEST)

        refusal = self._refuse_owner(request, owner_type, owner_id)
        if refusal is not None:
            return refusal

        try:
            asset = store_video(
                owner_type=owner_type, owner_id=owner_id, slot_key=slot_key,
                upload=upload, uploaded_by=request.user,
                alt_ar=request.data.get("alt_ar"), alt_en=request.data.get("alt_en"))
        except ValidationError as refused:
            return Response(
                {"error": refused.messages[0],
                 "code": getattr(refused, "code", None) or "invalid"},
                status=status.HTTP_400_BAD_REQUEST)

        from .services import payload
        return Response({"slot": asset.slot, "media": payload(asset, request)},
                        status=status.HTTP_201_CREATED)

    @staticmethod
    def _refuse_owner(request, owner_type, owner_id):
        """None when this caller may write to that owner, else the Response to send.

        The order matters for `exercise`, and it is the order `add-media/` already uses:
        "may you SEE it?" (no -> 404, indistinguishable from a missing id) before "may
        you CHANGE it?" (no -> 403). A 403 is only ever returned for a row the caller can
        already see, so it confirms nothing.
        """
        user = request.user
        is_admin = getattr(user, "is_admin", False) or user.is_staff

        if owner_type == OwnerType.TRAINER_EXERCISE:
            # A coach's own demonstration has ONE way in, and it is not this route:
            # `/api/v1/trainer/exercise-media/`. That one knows whose library it is
            # writing to and enforces the per-exercise cap; letting a second route
            # write the same rows would put the cap in two places, which is the same
            # as not having one. Refused for an admin too, for the same reason.
            return Response(
                {"error": _("Use /api/v1/trainer/exercise-media/ for your own "
                            "demonstration of an exercise.")},
                status=status.HTTP_403_FORBIDDEN)

        if is_admin:
            return None

        if owner_type == OwnerType.EXERCISE:
            exercise = _visible_exercise(request, owner_id)
            if exercise is None:
                return Response({"error": _("Exercise not found.")},
                                status=status.HTTP_404_NOT_FOUND)
            if not _may_modify_exercise(request, exercise):
                return Response(
                    {"error": _("You do not have permission to modify this exercise.")},
                    status=status.HTTP_403_FORBIDDEN)
            return None

        if owner_type == OwnerType.TRAINER:
            if getattr(user, "is_trainer", False) and int(owner_id) == user.id:
                return None
            return Response({"error": _("This is not yours to change.")},
                            status=status.HTTP_403_FORBIDDEN)

        # program / dish / app: the shared catalogue and the app's own content. Curated
        # in the operator console, never by a trainer.
        return Response({"error": _("This is not yours to change.")},
                        status=status.HTTP_403_FORBIDDEN)
