"""The per-trainer exercise library: a client watches THEIR coach, not a stock clip.

Before this, an exercise had one set of media and it belonged to the exercise row. A
coach who wanted their own demonstration had to copy the exercise, and a copy forks
every client's progress, records and adherence off the original id. So the exercise row
stays shared and a coach's footage hangs beside it, keyed by (trainer, exercise).

Which means "whose media?" is now a real question with three answers, and exactly one
place may answer it — `mediakit.resolution.resolve_exercise_media`. Every assertion
here is about that rule holding, or about the boundary around the library:

  * the three levels, in order, and a client seeing their own coach and not another's;
  * a trainer cannot attach to an exercise they may not SEE, and cannot delete the
    catalogue's media or another coach's — all answered with the same 404 an id that
    does not exist gets, byte for byte, so the route is not an oracle;
  * the per-exercise cap;
  * a day of eight exercises costs the same number of queries as a day of one.
"""
import io
import json
import struct

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

pytestmark = pytest.mark.django_db

LIST_URL = "/api/v1/trainer/exercise-media/"

# The view returns `{"error": ...}`; `normalize_error_envelope` fills in `detail` and
# the `code` the app branches on. Pinned whole, because "these two 404s are
# indistinguishable" is a claim about the bytes on the wire, not about the view's dict.
NOT_FOUND_BODY = {"error": "Exercise not found.", "detail": "Exercise not found.",
                  "code": "not_found"}


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Uploads land in a temporary directory, not in the repository's media/."""
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Builders. The video is bytes we assemble ourselves: the validator reads the
# BYTES, so a real .mp4 from disk would prove less, not more.
# ---------------------------------------------------------------------------

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def mp4_bytes(duration_seconds=4.0, filler=2048) -> bytes:
    mvhd = _box(b"mvhd", (b"\x00\x00\x00\x00" + struct.pack(">I", 0)
                          + struct.pack(">I", 0) + struct.pack(">I", 1000)
                          + struct.pack(">I", int(duration_seconds * 1000))
                          + b"\x00\x01\x00\x00" + b"\x01\x00" + b"\x00" * 10
                          + b"\x00" * 36 + b"\x00" * 24 + struct.pack(">I", 2)))
    return (_box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isom" + b"mp42")
            + _box(b"moov", mvhd) + _box(b"mdat", b"\x00" * filler))


def video_upload(name="clip.mp4"):
    return SimpleUploadedFile(name, mp4_bytes(), content_type="video/mp4")


def image_upload(w=1280, h=720, name="shot.jpg"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 60, 30)).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def upload_for(slot):
    """An upload the slot will actually accept: its own ratio, its own minimum size.

    Asked of `mediakit.slots` rather than written out here, so a slot whose ratio
    changes does not silently start testing `media_wrong_ratio` instead of the rule.
    """
    from mediakit.slots import VIDEO, resolve

    found = resolve(slot)
    if found.kind == VIDEO:
        return video_upload()
    width, height = found.min_size
    return image_upload(width, height)


def approve(trainer, client_user):
    """The approved relation — the platform's only source of truth for coach access.

    Written directly rather than through request/approve, because the rule under test
    reads this row and the endpoints drag the coaching hold into a media test.
    """
    from users.models import TrainerClientRelation

    return TrainerClientRelation.objects.create(
        trainer=trainer, client=client_user, status="approved", initiated_by="client")


def attach(trainer, exercise, slot="exercise.poster"):
    """One coach's demonstration, written through the service the endpoint uses."""
    from mediakit.services import store
    from mediakit.slots import OwnerType

    return store(owner_type=OwnerType.TRAINER_EXERCISE, owner_id=exercise.pk,
                 slot_key=slot, upload=upload_for(slot), person=trainer,
                 uploaded_by=trainer)


def catalogue_media(exercise, slot="exercise.poster"):
    from mediakit.services import store
    from mediakit.slots import OwnerType

    return store(owner_type=OwnerType.EXERCISE, owner_id=exercise.pk, slot_key=slot,
                 upload=upload_for(slot))


@pytest.fixture
def world(db, make_user):
    """Two coaches, one client of the first, the shared catalogue and one private row."""
    from routine.models import Exercise

    coach = make_user("tem-coach", user_type="trainer")
    other = make_user("tem-other", user_type="trainer")
    client = make_user("tem-client", user_type="client")
    admin = make_user("tem-admin", user_type="admin")
    approve(coach, client)

    squat = Exercise.objects.create(name="Back Squat")
    private = Exercise.objects.create(name="Other Coach Only", created_by=other)
    # `save()` derives is_global from ownership; asserted so a later change to that
    # derivation cannot quietly turn the private row into a catalogue one and make
    # every "refused" case below pass for the wrong reason.
    assert squat.is_global is True and private.is_global is False
    return {"coach": coach, "other": other, "client": client, "admin": admin,
            "squat": squat, "private": private}


def resolve(viewer, exercise, routine_author_id=None):
    from mediakit.resolution import resolve_exercise_media

    return resolve_exercise_media(viewer, [exercise.pk],
                                  routine_author_id=routine_author_id)[exercise.pk]


# ===========================================================================
# 1. The rule: three levels, in order
# ===========================================================================

def test_a_client_with_no_coach_sees_the_catalogue(world, make_user):
    """Line (c), and the floor under everything else."""
    alone = make_user("tem-alone", user_type="client")
    catalogue_media(world["squat"])

    answer = resolve(alone, world["squat"])

    assert answer["media_source"] == "catalogue"
    assert answer["media_trainer_id"] is None
    assert set(answer["media"]) == {"exercise.poster"}


def test_their_own_coachs_clip_beats_the_catalogue(world):
    """Line (b). The whole product claim: the client watches their coach."""
    catalogue_media(world["squat"])
    mine = attach(world["coach"], world["squat"])

    answer = resolve(world["client"], world["squat"])

    assert answer["media_source"] == "my_trainer"
    assert answer["media_trainer_id"] == world["coach"].pk
    assert answer["media"]["exercise.poster"]["url_1x"] == \
        _payload_url(mine), "the catalogue's still was served instead of the coach's"


def test_the_routine_author_beats_the_clients_current_coach(world):
    """Line (a) over line (b), the case where the two differ.

    A client may have only ONE approved coach at a time (`users` enforces
    `unique_approved_trainer_per_client`), but their old coach's routine stays
    readable — the owner's never-delete rule. Reading it, the demonstration has to be
    the one from the coach who wrote the work, not from whoever coaches them now.
    """
    catalogue_media(world["squat"])
    attach(world["coach"], world["squat"])
    theirs = attach(world["other"], world["squat"])

    answer = resolve(world["client"], world["squat"],
                     routine_author_id=world["other"].pk)

    assert answer["media_source"] == "routine_author"
    assert answer["media_trainer_id"] == world["other"].pk
    assert answer["media"]["exercise.poster"]["url_1x"] == _payload_url(theirs)


def test_the_order_holds_as_each_level_is_removed(world):
    """The three lines in sequence, on one exercise, with nothing else changing."""
    from mediakit.models import MediaAsset

    catalogue_media(world["squat"])
    mine = attach(world["coach"], world["squat"])
    author = attach(world["other"], world["squat"])

    first = resolve(world["client"], world["squat"], routine_author_id=world["other"].pk)
    assert first["media_source"] == "routine_author"

    MediaAsset.objects.filter(pk=author.pk).delete()
    second = resolve(world["client"], world["squat"], routine_author_id=world["other"].pk)
    assert second["media_source"] == "my_trainer"
    assert second["media_trainer_id"] == world["coach"].pk

    MediaAsset.objects.filter(pk=mine.pk).delete()
    third = resolve(world["client"], world["squat"], routine_author_id=world["other"].pk)
    assert third["media_source"] == "catalogue"


def test_a_client_never_sees_another_coachs_clip(world):
    """`other` has no relation to this client and did not write their routine. Their
    footage is not an alternative, a fallback, or anything else this client sees."""
    theirs = attach(world["other"], world["squat"])
    catalogue_media(world["squat"])

    answer = resolve(world["client"], world["squat"])

    assert answer["media_source"] == "catalogue"
    assert _payload_url(theirs) not in json.dumps(answer)


def test_a_client_with_no_coach_and_no_catalogue_gets_nothing(world, make_user):
    alone = make_user("tem-bare", user_type="client")
    attach(world["coach"], world["squat"])

    answer = resolve(alone, world["squat"])

    assert answer == {"media": {}, "media_source": None, "media_trainer_id": None}


def test_a_trainer_sees_their_own_library_over_the_catalogue(world):
    catalogue_media(world["squat"])
    attach(world["coach"], world["squat"])

    answer = resolve(world["coach"], world["squat"])

    assert answer["media_source"] == "my_trainer"
    assert answer["media_trainer_id"] == world["coach"].pk


def test_a_whole_source_wins_not_a_slot_at_a_time(world):
    """A coach who filmed only the loop shows their loop and NO poster. Mixing the
    two would put their clip over a stock still — two shoots, one card."""
    catalogue_media(world["squat"], slot="exercise.poster")
    catalogue_media(world["squat"], slot="exercise.thumb")
    attach(world["coach"], world["squat"], slot="exercise.thumb")

    answer = resolve(world["client"], world["squat"])

    assert answer["media_source"] == "my_trainer"
    assert set(answer["media"]) == {"exercise.thumb"}


def _payload_url(asset):
    from training_platform.media_storage import media_url

    return media_url(asset.file_1x, None)


# ===========================================================================
# 2. The library endpoints
# ===========================================================================

def test_a_trainer_lists_only_their_own_media_on_an_exercise(world, api):
    catalogue_media(world["squat"])
    attach(world["other"], world["squat"])
    attach(world["coach"], world["squat"], slot="exercise.thumb")

    response = api(world["coach"]).get(f"{LIST_URL}?exercise_id={world['squat'].pk}")

    assert response.status_code == 200, response.content
    body = response.json()
    assert set(body["media"]) == {"exercise.thumb"}
    assert (body["trainer_id"], body["count"], body["limit"]) == \
        (world["coach"].pk, 1, 3)


def test_a_trainer_attaches_an_image_and_a_video_to_a_catalogue_exercise(world, api):
    from mediakit.models import MediaAsset
    from mediakit.slots import OwnerType

    coach = api(world["coach"])
    picture = coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                    "slot": "exercise.poster", "file": image_upload()})
    clip = coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                 "slot": "exercise.loop", "file": video_upload()})

    assert picture.status_code == 201, picture.content
    assert clip.status_code == 201, clip.content
    assert picture.json()["media"]["url_1x"]
    # The video went through the platform's one video pipeline: `pending` until a
    # worker cuts the poster, playable immediately.
    assert clip.json()["media"]["state"] == "pending"
    assert clip.json()["media"]["video_url"]

    rows = MediaAsset.objects.filter(owner_type=OwnerType.TRAINER_EXERCISE,
                                     owner_id=world["squat"].pk)
    assert rows.count() == 2
    # The exercise row itself is untouched: no fork, so no split progress.
    assert not MediaAsset.objects.filter(owner_type=OwnerType.EXERCISE).exists()
    assert all(r.person_id == world["coach"].pk for r in rows)


def test_two_coaches_hold_the_same_slot_on_the_same_exercise(world, api):
    """The unique key had to change for this: keyed on the exercise alone, the first
    coach to film the back squat would have owned `exercise.poster` for everybody."""
    attach(world["coach"], world["squat"])
    attach(world["other"], world["squat"])

    assert resolve(world["client"], world["squat"])["media_trainer_id"] == \
        world["coach"].pk
    assert resolve(world["other"], world["squat"])["media_trainer_id"] == \
        world["other"].pk


def test_reuploading_a_filled_slot_replaces_it(world, api):
    from mediakit.models import MediaAsset

    coach = api(world["coach"])
    first = coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                  "slot": "exercise.poster", "file": image_upload()})
    second = coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                   "slot": "exercise.poster", "file": image_upload()})

    assert second.status_code == 201, second.content
    assert first.json()["id"] == second.json()["id"]
    assert MediaAsset.objects.filter(owner_id=world["squat"].pk).count() == 1


def test_a_client_has_no_library(world, api):
    response = api(world["client"]).post(
        LIST_URL, {"exercise_id": world["squat"].pk, "slot": "exercise.poster",
                   "file": image_upload()})

    assert response.status_code == 403
    assert response.json()["code"] == "not_a_trainer"


def test_an_admin_acts_for_the_trainer_they_name(world, api):
    from mediakit.models import MediaAsset

    response = api(world["admin"]).post(
        LIST_URL, {"exercise_id": world["squat"].pk, "slot": "exercise.poster",
                   "trainer_id": world["coach"].pk, "file": image_upload()})

    assert response.status_code == 201, response.content
    assert MediaAsset.objects.get(owner_id=world["squat"].pk).person_id == \
        world["coach"].pk
    # Naming nobody is not "act as yourself": an admin has no library of their own.
    assert api(world["admin"]).get(f"{LIST_URL}?exercise_id={world['squat'].pk}") \
        .status_code == 403


def test_the_slot_must_be_one_an_exercise_has(world, api):
    response = api(world["coach"]).post(
        LIST_URL, {"exercise_id": world["squat"].pk, "slot": "trainer.portrait",
                   "file": image_upload()})

    assert response.status_code == 400
    assert response.json()["code"] == "media_unknown_slot"


def test_the_generic_video_route_refuses_this_owner_type(world, api):
    """One way in, so the cap lives in one place."""
    response = api(world["coach"]).post("/api/v1/content/media/video/", {
        "owner_type": "trainer_exercise", "owner_id": world["squat"].pk,
        "slot": "exercise.loop", "file": video_upload()})

    assert response.status_code == 403, response.content


# ===========================================================================
# 3. The boundary: you may only touch an exercise you may SEE
# ===========================================================================

def test_a_trainer_cannot_attach_to_an_exercise_they_may_not_see(world, api):
    response = api(world["coach"]).post(
        LIST_URL, {"exercise_id": world["private"].pk, "slot": "exercise.poster",
                   "file": image_upload()})

    assert response.status_code == 404, response.content
    assert json.loads(response.content) == NOT_FOUND_BODY


def test_an_invisible_exercise_is_indistinguishable_from_a_missing_one(world, api):
    """Status AND body, or the route answers "does exercise 41 exist?" to anyone who
    asks twice."""
    from routine.models import Exercise

    missing = Exercise.objects.order_by("-id").first().pk + 1000
    coach = api(world["coach"])

    invisible = coach.get(f"{LIST_URL}?exercise_id={world['private'].pk}")
    absent = coach.get(f"{LIST_URL}?exercise_id={missing}")

    assert invisible.status_code == absent.status_code == 404
    assert json.loads(invisible.content) == json.loads(absent.content) == NOT_FOUND_BODY


def test_an_asset_is_exactly_as_visible_as_the_exercise_it_hangs_on(world, api):
    """The asset id is not a second door. A demonstration on an exercise this caller
    may not see answers the SAME bytes as an asset id that never existed."""
    from mediakit.models import MediaAsset

    hidden = attach(world["other"], world["private"])
    missing = MediaAsset.objects.order_by("-id").first().pk + 1000
    coach = api(world["coach"])

    on_hidden = coach.delete(f"{LIST_URL}{hidden.pk}/")
    absent = coach.delete(f"{LIST_URL}{missing}/")

    assert on_hidden.status_code == absent.status_code == 404
    assert json.loads(on_hidden.content) == json.loads(absent.content) == NOT_FOUND_BODY
    assert MediaAsset.objects.filter(pk=hidden.pk).exists()


def test_a_trainer_cannot_delete_another_coachs_demonstration(world, api):
    from mediakit.models import MediaAsset

    theirs = attach(world["other"], world["squat"])

    response = api(world["coach"]).delete(f"{LIST_URL}{theirs.pk}/")

    assert response.status_code == 404, response.content
    assert json.loads(response.content) == NOT_FOUND_BODY
    assert MediaAsset.objects.filter(pk=theirs.pk).exists()


def test_the_catalogues_media_is_not_reachable_from_this_route(world, api):
    """Deleting one would take it away from every client of every coach. It is the
    platform's, curated in the console, and a 404 here says nothing about it."""
    from mediakit.models import MediaAsset

    shared = catalogue_media(world["squat"])

    refused = api(world["coach"]).delete(f"{LIST_URL}{shared.pk}/")
    admin_too = api(world["admin"]).delete(f"{LIST_URL}{shared.pk}/")

    assert refused.status_code == admin_too.status_code == 404
    assert json.loads(refused.content) == NOT_FOUND_BODY
    assert MediaAsset.objects.filter(pk=shared.pk).exists()


def test_a_trainer_deletes_their_own(world, api):
    from mediakit.models import MediaAsset

    mine = attach(world["coach"], world["squat"])

    response = api(world["coach"]).delete(f"{LIST_URL}{mine.pk}/")

    assert response.status_code == 204, response.content
    assert not MediaAsset.objects.filter(pk=mine.pk).exists()


# ===========================================================================
# 4. The cap
# ===========================================================================

def test_a_fourth_slot_on_one_exercise_is_refused(world, api):
    coach = api(world["coach"])
    for slot in ("exercise.poster", "exercise.thumb", "exercise.loop"):
        sent = coach.post(LIST_URL, {"exercise_id": world["squat"].pk, "slot": slot,
                                     "file": upload_for(slot)})
        assert sent.status_code == 201, (slot, sent.content)

    fourth = coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                   "slot": "exercise.cue", "file": video_upload()})

    assert fourth.status_code == 400, fourth.content
    assert fourth.json()["code"] == "media_library_full"


def test_the_cap_does_not_stop_a_coach_correcting_their_own_footage(world, api, settings):
    """Replacing a filled slot is not a new asset. Charging it would leave a coach
    holding three clips they cannot fix."""
    settings.MEDIA_TRAINER_EXERCISE_MAX_ASSETS = 1
    coach = api(world["coach"])
    assert coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                 "slot": "exercise.poster",
                                 "file": image_upload()}).status_code == 201

    again = coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                  "slot": "exercise.poster", "file": image_upload()})
    another = coach.post(LIST_URL, {"exercise_id": world["squat"].pk,
                                    "slot": "exercise.thumb", "file": image_upload()})

    assert again.status_code == 201, again.content
    assert another.status_code == 400
    assert another.json()["code"] == "media_library_full"


def test_the_cap_is_per_exercise_not_per_trainer(world, api):
    """A coach with three clips on the squat still has a whole library everywhere
    else — the limit is what one exercise costs, not what one coach may own."""
    from routine.models import Exercise

    bench = Exercise.objects.create(name="Bench Press")
    coach = api(world["coach"])
    for slot in ("exercise.poster", "exercise.thumb", "exercise.cue"):
        coach.post(LIST_URL, {"exercise_id": world["squat"].pk, "slot": slot,
                              "file": upload_for(slot)})

    elsewhere = coach.post(LIST_URL, {"exercise_id": bench.pk,
                                      "slot": "exercise.poster", "file": image_upload()})

    assert elsewhere.status_code == 201, elsewhere.content


# ===========================================================================
# 5. Serving: the day, and what it costs
# ===========================================================================

@pytest.fixture
def day(world):
    """One routine by the client's coach, eight exercises on day 1, each with the
    coach's own demonstration and the catalogue's."""
    from routine.models import Exercise, Routine, RoutineExercise

    routine = Routine.objects.create(name="Push day", created_by=world["coach"])
    routine.assigned_to.add(world["client"])
    exercises = []
    for n in range(8):
        exercise = Exercise.objects.create(name=f"Move {n}")
        catalogue_media(exercise)
        attach(world["coach"], exercise, slot="exercise.thumb")
        RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1,
                                       order=n + 1)
        exercises.append(exercise)
    return {"routine": routine, "exercises": exercises}


def test_the_routine_day_serves_the_coachs_media_through_the_rule(world, day, api):
    response = api(world["client"]).get(f"/api/routine/routines/{day['routine'].pk}/")

    assert response.status_code == 200, response.content
    rows = response.json()["routine_exercises"]
    assert len(rows) == 8
    for row in rows:
        exercise = row["exercise"]
        assert exercise["media_source"] == "routine_author", exercise["id"]
        assert exercise["media_trainer_id"] == world["coach"].pk
        assert set(exercise["media_slots"]) == {"exercise.thumb"}
        # Additive: the legacy list is still there and still means what it meant.
        assert exercise["media"] == []


def test_a_day_of_eight_costs_what_a_day_of_one_costs(world, day, api,
                                                      django_assert_num_queries):
    """The point of the batch. Resolving per exercise would be one query each, and a
    day is where the app spends its time.

    Measured as a DIFFERENCE, not a fixed number: the routine payload does plenty
    besides media and pinning its total would fail on any unrelated change.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from routine.models import Exercise, Routine, RoutineExercise

    # A second routine, of one exercise, built the same way. Two routines rather than
    # one routine shrunk in place: deleting rows mid-test invalidates the ORM cache,
    # so the "after" run would re-issue queries the "before" run was served from
    # memory and the difference would measure cachalot, not the batch.
    single = Routine.objects.create(name="Single", created_by=world["coach"])
    single.assigned_to.add(world["client"])
    lonely = Exercise.objects.create(name="Only Move")
    catalogue_media(lonely)
    attach(world["coach"], lonely, slot="exercise.thumb")
    RoutineExercise.objects.create(routine=single, exercise=lonely, day=1, order=1)

    client = api(world["client"])
    wide_url = f"/api/routine/routines/{day['routine'].pk}/"
    narrow_url = f"/api/routine/routines/{single.pk}/"
    # Warm both first, so each measurement is of a warm request.
    assert client.get(wide_url).status_code == 200
    assert client.get(narrow_url).status_code == 200

    with CaptureQueriesContext(connection) as wide:
        assert client.get(wide_url).status_code == 200
    with CaptureQueriesContext(connection) as narrow:
        assert client.get(narrow_url).status_code == 200

    assert len(wide) == len(narrow), (
        f"eight exercises cost {len(wide) - len(narrow)} queries more than one: "
        + "\n".join(q["sql"][:140] for q in wide.captured_queries))

    # And the assets themselves are fetched once, not once per exercise.
    reads = [q for q in wide.captured_queries if "mediakit_mediaasset" in q["sql"]]
    assert len(reads) == 1, [q["sql"][:200] for q in reads]


def test_the_exercise_detail_and_the_media_route_agree(world, api):
    """Two doors onto the same fact. If they could disagree, the app would show one
    coach on the card and another on the player."""
    catalogue_media(world["squat"])
    attach(world["coach"], world["squat"])
    client = api(world["client"])

    detail = client.get(f"/api/routine/exercises/{world['squat'].pk}/").json()
    standalone = client.get(
        f"/api/v1/content/media/exercise/{world['squat'].pk}/").json()

    assert detail["media_source"] == standalone["media_source"] == "my_trainer"
    assert detail["media_trainer_id"] == standalone["media_trainer_id"] == \
        world["coach"].pk
    assert set(detail["media_slots"]) == set(standalone["media"])


def test_the_media_route_honours_the_routine_it_was_opened_from(world, day, api):
    """`?routine_id=` is line (a) for a screen that has no routine payload around it."""
    exercise = day["exercises"][0]
    client = api(world["client"])

    plain = client.get(f"/api/v1/content/media/exercise/{exercise.pk}/").json()
    in_routine = client.get(
        f"/api/v1/content/media/exercise/{exercise.pk}/"
        f"?routine_id={day['routine'].pk}").json()

    assert plain["media_source"] == "my_trainer"
    assert in_routine["media_source"] == "routine_author"
    # A routine the caller is not part of changes nothing, and is not an error: it
    # must not become a way to ask who wrote routine 41.
    stranger = api(world["other"]).get(
        f"/api/v1/content/media/exercise/{exercise.pk}/"
        f"?routine_id={day['routine'].pk}")
    assert stranger.status_code == 200
    assert stranger.json()["media_source"] == "catalogue"


def test_the_day_checklist_carries_the_resolved_media(world, api):
    """`user-exercise-progress/daily-summary/` — today's workout."""
    from django.utils import timezone

    from routine.models import UserExerciseProgress

    catalogue_media(world["squat"])
    attach(world["coach"], world["squat"])
    UserExerciseProgress.objects.create(user=world["client"], exercise=world["squat"],
                                        date=timezone.localdate(), target_sets=3)

    response = api(world["client"]).get(
        "/api/routine/user-exercise-progress/daily-summary/")

    assert response.status_code == 200, response.content
    row = response.json()["exercises"][0]["exercise"]
    assert row["media_source"] == "my_trainer"
    assert row["media_trainer_id"] == world["coach"].pk


def test_a_public_template_never_shows_a_stranger_coachs_footage(world, api):
    """A template is not a routine: nobody prescribed it to the person reading it, so
    line (a) does not apply and its author's private footage stays theirs."""
    from routine.models import RoutineTemplate, RoutineTemplateExercise

    catalogue_media(world["squat"])
    theirs = attach(world["other"], world["squat"])
    template = RoutineTemplate.objects.create(name="Starter", goal="strength",
                                              created_by=world["other"],
                                              is_public=True)
    RoutineTemplateExercise.objects.create(template=template, exercise=world["squat"],
                                           day=1, order=1)

    response = api(world["client"]).get(f"/api/routine/templates/{template.pk}/")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["exercises"][0]["exercise"]["media_source"] == "catalogue"
    assert _payload_url(theirs) not in json.dumps(body)


# ===========================================================================
# 6. Wiring
# ===========================================================================

def test_every_new_route_is_in_the_contract():
    from pathlib import Path

    contract = (Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text()
    for path in ("/api/v1/trainer/exercise-media/",
                 "/api/v1/trainer/exercise-media/{id}/"):
        assert path in contract, f"{path} is not in API_CONTRACT.md"


def test_every_refusal_code_this_route_can_answer_is_documented():
    """The app branches on `code` and on nothing else, so a code that exists only in
    the source is a branch the client cannot write."""
    from pathlib import Path

    contract = (Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text()
    for code in ("media_exercise_id_required", "media_file_required",
                 "media_unknown_slot", "media_library_full", "not_a_trainer",
                 "media_slots", "media_source", "media_trainer_id"):
        assert code in contract, f"{code} is not documented"


def test_the_arabic_translation_is_compiled_and_covers_the_new_strings():
    """Arabic is the primary language: an untranslated refusal is an English sentence
    in the middle of an Arabic screen."""
    from pathlib import Path

    from django.utils import translation

    root = Path(__file__).resolve().parent.parent
    po = (root / "locale" / "ar" / "LC_MESSAGES" / "django.po").read_text()
    assert (root / "locale" / "ar" / "LC_MESSAGES" / "django.mo").exists()

    for english in ("Name the exercise as `exercise_id`.",
                    "Attach the file as `file`.",
                    "Only a trainer has a library here."):
        assert f'msgid "{english}"' in po, f"{english!r} has no Arabic entry"
        with translation.override("ar"):
            assert str(translation.gettext(english)) != english, \
                f"{english!r} is not translated in the compiled catalogue"


def test_a_coachs_demonstration_is_personal_data_the_registry_can_erase(world):
    """Their footage of an exercise is as much theirs as their portrait, and erasure
    has to take it. `person` is what the registry reads, which is why the check
    constraint insists on it for this owner type too."""
    from mediakit.models import MediaAsset
    from training_platform.privacy.registry import registry

    mine = attach(world["coach"], world["squat"])
    source = registry["mediakit.MediaAsset"]

    assert source.on_erase == "delete"
    assert source.queryset_for(world["coach"]).filter(pk=mine.pk).exists()
    # And it is not merely findable: the row is gone with the person. Erasure runs
    # through the registry rather than `user.delete()`, which a wallet's PROTECT stops
    # (the ledger outlives the account — `release_holds_before_erasure`).
    source.queryset_for(world["coach"]).delete()
    assert not MediaAsset.objects.filter(pk=mine.pk).exists()
    # The exercise it hung on is untouched: erasing a coach must not take the shared
    # catalogue's row with it.
    assert world["squat"].__class__.objects.filter(pk=world["squat"].pk).exists()


def test_the_database_refuses_a_demonstration_with_no_trainer():
    """The constraint, not the view. A row with no `person` would be a demonstration
    belonging to nobody: invisible to the rule and unreachable by erasure."""
    from django.db import IntegrityError, transaction

    from mediakit.models import MediaAsset
    from routine.models import Exercise

    exercise = Exercise.objects.create(name="Constraint Row")
    with pytest.raises(IntegrityError), transaction.atomic():
        MediaAsset.objects.create(owner_type="trainer_exercise", owner_id=exercise.pk,
                                  slot="exercise.poster", person=None)
