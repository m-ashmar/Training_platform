"""`/api/routine/exercises/{id}/add-media/` answers only for exercises you may see.

The read-only review of 2026-09-20 found `ExerciseAddMediaView.get` resolving the id
with a bare `Exercise.objects.get(id=exercise_id)`. The exercise list and detail have
scoped visibility for a while (`ExerciseViewSet.get_queryset`), but this view resolved
ids for itself and so had no rule at all: any signed-in account could read any
trainer's private exercise media — the photo and video URLs and the written form tips
a coach builds their programme from — by walking integer ids.

Two separate questions, and the order matters:

  * may this caller SEE the exercise? — `routine.permissions.visible_exercises_for`,
    the same rule the list uses. No -> `404 {"error": "Exercise not found"}`, byte for
    byte what a missing id answers, so the endpoint cannot be used to discover which
    ids exist;
  * may this caller CHANGE it? — owner or admin. No -> 403. A 403 is only ever
    returned for a row the caller can already see, so it confirms nothing.
"""
import json

import pytest


# The view returns `{"error": ...}`; `normalize_error_envelope` fills in `detail` and
# the `code` the app branches on. Pinned whole, because "the two 404s are
# indistinguishable" is a claim about the bytes on the wire, not about the view's dict.
# The text is `_("Exercise not found.")` since 2026-09-20: every user-facing string
# goes through gettext (CLAUDE.md), and this one is served to a client. The CODE is
# what the app branches on and it is unchanged — and so is the point of this file, that
# both 404s are the same bytes.
NOT_FOUND_BODY = {"error": "Exercise not found.", "detail": "Exercise not found.",
                  "code": "not_found"}

MEDIA_PAYLOAD = {
    "media_items": [
        {"media_type": "video", "content": "https://videos.test/secret-cue.mp4",
         "title": "Coach cue", "description": "private", "order": 1},
    ]
}


def url(exercise):
    return f"/api/routine/exercises/{getattr(exercise, 'id', exercise)}/add-media/"


def approve(trainer, client_user):
    """The approved relation — the platform's only source of truth for trainer access.

    Written directly rather than through the request/approve endpoints because the rule
    under test reads this row; the endpoints' own behaviour is covered elsewhere and
    drags the coaching hold into a visibility test.
    """
    from users.models import TrainerClientRelation

    return TrainerClientRelation.objects.create(
        trainer=trainer, client=client_user, status="approved", initiated_by="client",
    )


@pytest.fixture
def world(db, make_user):
    """One owner trainer with a private exercise, a stranger trainer, and the catalogue."""
    from routine.models import Exercise, ExerciseMedia

    owner = make_user("media-owner", user_type="trainer")
    stranger = make_user("media-stranger", user_type="trainer")
    admin = make_user("media-admin", user_type="admin")

    private = Exercise.objects.create(name="Owner Only Press", created_by=owner)
    ExerciseMedia.objects.create(
        exercise=private, media_type="video",
        content="https://videos.test/owner-only.mp4", title="Owner tutorial", order=1)

    catalogue = Exercise.objects.create(name="Catalogue Squat")
    ExerciseMedia.objects.create(
        exercise=catalogue, media_type="text", content="Brace, then sit down.",
        title="Form tips", order=1)

    # `save()` derives is_global from ownership; assert it so a later change to that
    # derivation cannot quietly turn the private exercise into a catalogue row and make
    # every "stranger is refused" case below pass for the wrong reason.
    assert private.is_global is False and catalogue.is_global is True
    return {"owner": owner, "stranger": stranger, "admin": admin,
            "private": private, "catalogue": catalogue}


# ---------------------------------------------------------------------------
# GET — the leak
# ---------------------------------------------------------------------------

def test_a_stranger_trainer_cannot_read_another_trainers_private_media(world, api):
    """The finding itself: this returned 200 with the owner's video URLs."""
    r = api(world["stranger"]).get(url(world["private"]))

    assert r.status_code == 404, r.content
    body = json.loads(r.content)
    assert body == NOT_FOUND_BODY
    assert "owner-only.mp4" not in r.content.decode()


def test_an_invisible_exercise_is_indistinguishable_from_a_missing_one(world, api):
    """Status AND body match, or the endpoint becomes an id oracle: 404-with-one-body
    for "not yours" and 404-with-another for "no such row" still answers the question
    "does exercise 41 exist?" for anyone who asks twice."""
    from routine.models import Exercise

    missing_id = Exercise.objects.order_by("-id").first().id + 1000
    stranger = api(world["stranger"])

    invisible = stranger.get(url(world["private"]))
    missing = stranger.get(url(missing_id))

    assert invisible.status_code == missing.status_code == 404
    assert json.loads(invisible.content) == json.loads(missing.content) == NOT_FOUND_BODY


def test_a_client_of_another_trainer_cannot_read_private_media(world, api, make_user):
    outsider = make_user("media-outsider", user_type="client")
    approve(world["stranger"], outsider)

    r = api(outsider).get(url(world["private"]))

    assert r.status_code == 404, r.content
    assert json.loads(r.content) == NOT_FOUND_BODY


def test_the_owner_reads_their_own_media(world, api):
    r = api(world["owner"]).get(url(world["private"]))

    assert r.status_code == 200, r.content
    body = json.loads(r.content)
    assert body["exercise_id"] == world["private"].id
    assert body["media_count"] == 1
    assert body["media_items"][0]["content"] == "https://videos.test/owner-only.mp4"


def test_an_approved_client_reads_their_trainers_media(world, api, make_user):
    """The client has to see the tutorial their coach attached — that is the feature."""
    client_user = make_user("media-client", user_type="client")
    approve(world["owner"], client_user)

    r = api(client_user).get(url(world["private"]))

    assert r.status_code == 200, r.content
    assert json.loads(r.content)["media_count"] == 1


def test_a_client_reads_the_media_of_an_exercise_in_a_routine_assigned_to_them(
        world, api, make_user):
    """No approved relation — an ex-client keeps the routine they were given, and the
    third arm of the visibility rule is what keeps its videos playable."""
    from routine.models import Routine, RoutineExercise

    client_user = make_user("media-assigned", user_type="client")
    routine = Routine.objects.create(name="Handed Over", days=3, created_by=world["owner"])
    routine.assigned_to.set([client_user])
    RoutineExercise.objects.create(routine=routine, exercise=world["private"],
                                   sets=3, reps=10, day=1, order=1)

    r = api(client_user).get(url(world["private"]))

    assert r.status_code == 200, r.content
    assert json.loads(r.content)["media_count"] == 1


@pytest.mark.parametrize("who", ["owner", "stranger", "admin"])
def test_the_shared_catalogue_is_readable_by_everyone(world, api, who):
    r = api(world[who]).get(url(world["catalogue"]))

    assert r.status_code == 200, r.content
    assert json.loads(r.content)["media_items"][0]["content"] == "Brace, then sit down."


def test_a_plain_client_reads_the_shared_catalogue(world, api, make_user):
    unrelated = make_user("media-nobody", user_type="client")

    r = api(unrelated).get(url(world["catalogue"]))

    assert r.status_code == 200, r.content
    assert json.loads(r.content)["media_count"] == 1


def test_an_admin_reads_any_trainers_media(world, api):
    r = api(world["admin"]).get(url(world["private"]))

    assert r.status_code == 200, r.content
    assert json.loads(r.content)["media_count"] == 1


def test_media_needs_a_signed_in_caller(world, api):
    r = api().get(url(world["private"]))

    assert r.status_code in (401, 403), r.content
    assert "owner-only.mp4" not in r.content.decode()


# ---------------------------------------------------------------------------
# POST / DELETE — owner only, and still no id oracle
# ---------------------------------------------------------------------------

def test_a_stranger_trainer_writing_to_a_private_exercise_is_answered_as_missing(world, api):
    """404, not 403: a 403 here would confirm the exercise exists just as well as the
    old GET did, and the caller is not entitled to that either."""
    from routine.models import ExerciseMedia

    r = api(world["stranger"]).post(url(world["private"]), data=json.dumps(MEDIA_PAYLOAD),
                                    content_type="application/json")

    assert r.status_code == 404, r.content
    assert json.loads(r.content) == NOT_FOUND_BODY
    assert ExerciseMedia.objects.filter(exercise=world["private"]).count() == 1


def test_a_stranger_trainer_cannot_delete_media_it_cannot_see(world, api):
    from routine.models import ExerciseMedia

    media = ExerciseMedia.objects.get(exercise=world["private"])
    r = api(world["stranger"]).delete(url(world["private"]),
                                      data=json.dumps({"media_ids": [media.id]}),
                                      content_type="application/json")

    assert r.status_code == 404, r.content
    assert ExerciseMedia.objects.filter(pk=media.pk).exists()


def test_seeing_an_exercise_is_not_editing_it(world, api, make_user):
    """The approved client reads the media (200 above) and is still refused the write."""
    from routine.models import ExerciseMedia

    client_user = make_user("media-reader", user_type="client")
    approve(world["owner"], client_user)

    r = api(client_user).post(url(world["private"]), data=json.dumps(MEDIA_PAYLOAD),
                              content_type="application/json")

    assert r.status_code == 403, r.content
    assert ExerciseMedia.objects.filter(exercise=world["private"]).count() == 1


@pytest.mark.parametrize("who", ["owner", "stranger"])
def test_the_shared_catalogue_is_not_writable_by_a_trainer(world, api, who):
    """Visible to all, owned by nobody, editable only by an admin."""
    from routine.models import ExerciseMedia

    r = api(world[who]).post(url(world["catalogue"]), data=json.dumps(MEDIA_PAYLOAD),
                             content_type="application/json")

    assert r.status_code == 403, r.content
    assert ExerciseMedia.objects.filter(exercise=world["catalogue"]).count() == 1


def test_the_owner_adds_and_removes_their_own_media(world, api):
    from routine.models import ExerciseMedia

    owner_api = api(world["owner"])
    r = owner_api.post(url(world["private"]), data=json.dumps(MEDIA_PAYLOAD),
                       content_type="application/json")
    assert r.status_code == 201, r.content
    added = json.loads(r.content)["created_media"][0]["id"]
    assert ExerciseMedia.objects.filter(exercise=world["private"]).count() == 2

    r = owner_api.delete(url(world["private"]), data=json.dumps({"media_ids": [added]}),
                         content_type="application/json")
    assert r.status_code == 200, r.content
    assert not ExerciseMedia.objects.filter(pk=added).exists()


def test_an_admin_curates_the_shared_catalogue(world, api):
    from routine.models import ExerciseMedia

    r = api(world["admin"]).post(url(world["catalogue"]), data=json.dumps(MEDIA_PAYLOAD),
                                 content_type="application/json")

    assert r.status_code == 201, r.content
    assert ExerciseMedia.objects.filter(exercise=world["catalogue"]).count() == 2


# ---------------------------------------------------------------------------
# The rule now has one home; the list must still enforce it
# ---------------------------------------------------------------------------

def test_the_exercise_list_still_hides_another_trainers_exercise(world, api):
    """`ExerciseViewSet.get_queryset` was rewritten to call the shared rule. This is the
    behaviour it had before, pinned so the extraction cannot have widened it."""
    r = api(world["stranger"]).get("/api/routine/exercises/?page_size=200")

    assert r.status_code == 200, r.content
    names = [e["name"] for e in json.loads(r.content).get("results", [])]
    assert "Catalogue Squat" in names
    assert "Owner Only Press" not in names


def test_an_approved_client_sees_the_catalogue_and_their_trainers_exercises(
        world, api, make_user):
    client_user = make_user("media-lister", user_type="client")
    approve(world["owner"], client_user)

    r = api(client_user).get("/api/routine/exercises/?page_size=200")

    assert r.status_code == 200, r.content
    names = [e["name"] for e in json.loads(r.content).get("results", [])]
    assert "Catalogue Squat" in names
    assert "Owner Only Press" in names
