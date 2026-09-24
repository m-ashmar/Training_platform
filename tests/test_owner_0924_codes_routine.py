"""Every refusal of the routine app names its own `code` (the owner, 2026-09-24, decision 9).

"Give generic API refusals their own specific error codes. Do it now; finalize the
contract before the Flutter integration gets deeper." These refusals answered only their
status default — the `bad_request`, `permission_denied`, `not_found` or `conflict` the
error envelope fills in when a view names none — so the app could not tell "no client_id"
from "a date in the future", or "not your routine" from "you are not a trainer".
API_CONTRACT.md §6 lets such a refusal gain a code of its own; a code a view already
NAMED never changes, and no test here asks for one to.

Each test fails on the code before this change for the reason its docstring gives: the
generic code where a specific one is asserted, a missing key, or — for a date shaped like
a date and not one — an unhandled ValueError (a 500).
"""
import ast
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from tests.test_fx_rttr import _program, _progress, _session, _set

pytestmark = pytest.mark.django_db

JSON = "application/json"
ARABIC = re.compile(r"[؀-ۿ]")
TODAY = lambda: timezone.localdate()  # noqa: E731 — read at call time, never at import


def _code(resp):
    return resp.json().get("code")


def _strangers_routine(make_user, tag):
    from routine.models import Exercise, Routine, RoutineExercise

    stranger = make_user(f"oc_stranger_{tag}", user_type="trainer")
    routine = Routine.objects.create(name=f"oc theirs {tag}", created_by=stranger, days=1,
                                     start_date=timezone.localdate())
    exercise = Exercise.objects.create(name=f"oc theirs ex {tag}", created_by=stranger)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, order=1,
                                   sets=3, reps=10)
    return routine


def _with_an_empty_third_day(routine):
    """Days 1 and 2 hold an exercise each (`_program(days=2)`); day 3 exists and holds
    nothing."""
    from routine.models import Routine

    Routine.objects.filter(pk=routine.pk).update(days=3)
    routine.refresh_from_db()
    return routine


# ------------------------------------------------------------------ delete conflicts
def test_the_two_delete_conflicts_name_their_reason(make_user, api):
    """Both answered 409 with no code, which the envelope fills as `conflict`."""
    from routine.models import RoutineProgress

    trainer, client, routine, rows = _program(make_user, "oc_del")
    _progress(client, rows[0])
    RoutineProgress.objects.get_or_create(user=client, routine=routine, day=1,
                                          date=TODAY())

    exercise = api(trainer).delete(f"/api/routine/exercises/{rows[0].exercise_id}/")
    assert exercise.status_code == 409, exercise.content
    assert _code(exercise) == "exercise_has_history"

    routine_resp = api(trainer).delete(f"/api/routine/routines/{routine.pk}/")
    assert routine_resp.status_code == 409, routine_resp.content
    assert _code(routine_resp) == "routine_has_history"


def test_create_with_image_names_the_missing_field(make_user, api):
    """`Missing required field: name` answered `bad_request`, with the field only inside
    a translated sentence."""
    trainer = make_user("oc_t_cwi", user_type="trainer")
    resp = api(trainer).post("/api/routine/exercises/create-with-image/",
                             {"description": "d", "target_muscle": "Biceps"})
    assert resp.status_code == 400, resp.content
    assert (_code(resp), resp.json()["field"]) == ("field_required", "name")


# ------------------------------------------------------------ assign / unassign
def test_assign_to_client_names_every_refusal(make_user, api):
    """`client_id` missing, not your client, already yours and no such client answered
    `bad_request` / `not_found` — and in English whatever the client spoke."""
    trainer, client, routine, rows = _program(make_user, "oc_as", coached=True,
                                              assign=False)
    other = make_user("oc_c_as_other")
    url = f"/api/routine/routines/{routine.pk}/assign_to_client/"
    c = api(trainer)

    missing = c.post(url, {}, content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")
    assert (missing.status_code, _code(missing)) == (400, "client_id_required")
    assert ARABIC.search(missing.json()["error"]), missing.json()

    nobody = c.post(url, {"client_id": other.pk + 10_000}, content_type=JSON)
    assert (nobody.status_code, _code(nobody)) == (404, "client_not_found")

    not_mine = c.post(url, {"client_id": other.pk}, content_type=JSON)
    assert (not_mine.status_code, _code(not_mine)) == (400, "not_your_client")

    routine.assigned_to.add(client)
    again = c.post(url, {"client_id": client.pk}, content_type=JSON)
    assert (again.status_code, _code(again)) == (400, "already_assigned")
    assert (again.json()["routine_id"], again.json()["client_id"]) == (routine.pk, client.pk)


def test_assign_to_client_names_its_500(make_user, api, monkeypatch):
    """A fault nobody anticipated answered `{"error": "An error occurred..."}` and the
    envelope's `server_error`, in English."""
    from routine import services as routine_services

    trainer, client, routine, rows = _program(make_user, "oc_as500", coached=True,
                                              assign=False)

    def broken(*args, **kwargs):
        raise RuntimeError("the push service is down")

    monkeypatch.setattr(routine_services, "notify_assignment", broken)
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                             {"client_id": client.pk}, content_type=JSON)
    assert (resp.status_code, _code(resp)) == (500, "routine_assign_failed")
    assert "push service" not in resp.content.decode()


def test_unassign_from_client_names_every_refusal(make_user, api):
    """The same four refusals on the way out answered `bad_request` / `not_found`."""
    trainer, client, routine, rows = _program(make_user, "oc_un", coached=True,
                                              assign=False)
    other = make_user("oc_c_un_other")
    url = f"/api/routine/routines/{routine.pk}/unassign_from_client/"
    c = api(trainer)

    assert _code(c.post(url, {}, content_type=JSON)) == "client_id_required"
    nobody = c.post(url, {"client_id": other.pk + 10_000}, content_type=JSON)
    assert (nobody.status_code, _code(nobody)) == (404, "client_not_found")
    not_mine = c.post(url, {"client_id": other.pk}, content_type=JSON)
    assert (not_mine.status_code, _code(not_mine)) == (400, "not_your_client")
    not_on_it = c.post(url, {"client_id": client.pk}, content_type=JSON)
    assert (not_on_it.status_code, _code(not_on_it)) == (400, "not_assigned")


# ------------------------------------------------------------------- update_progress
def test_update_progress_names_every_refusal(make_user, api):
    """Missing fields, a day that is not a number, a day outside the routine and a day
    with nothing in it all answered `bad_request`; someone else's routine answered
    `permission_denied`."""
    trainer, client, routine, rows = _program(make_user, "oc_up", days=2)
    _with_an_empty_third_day(routine)
    url = f"/api/routine/routines/{routine.pk}/update_progress/"
    c = api(client)

    cases = [
        ({}, "day_and_status_required"),
        ({"day": "abc", "status": "in_progress"}, "bad_day"),
        ({"day": 9, "status": "in_progress"}, "day_out_of_range"),
        ({"day": 3, "status": "in_progress"}, "day_has_no_exercises"),
    ]
    for body, code in cases:
        resp = c.post(url, body, content_type=JSON)
        assert (resp.status_code, _code(resp)) == (400, code), (body, resp.content)

    theirs = api(trainer).post(url, {"day": 1, "status": "in_progress"}, content_type=JSON)
    assert (theirs.status_code, _code(theirs)) == (403, "routine_not_yours")


# ---------------------------------------------------------------- trainer-only routes
def test_a_trainer_only_route_says_not_a_trainer(make_user, api):
    """Four routes refused a non-trainer with the generic `permission_denied` — the same
    code as a wrong-role refusal anywhere, and as "not your exercise"."""
    admin = make_user("oc_admin_roles", user_type="admin")
    client = make_user("oc_c_roles")

    for user, path in (
            (admin, "/api/routine/routines/my_clients_progress/"),
            (client, "/api/routine/analytics/admin_dashboard/"),
            (client, "/api/routine/templates/my_templates/"),
            (admin, "/api/routine/trainer/client-progress/recent/")):
        resp = api(user).get(path)
        assert (resp.status_code, _code(resp)) == (403, "not_a_trainer"), (path, resp.content)


# ----------------------------------------------------------------- progress and sets
def test_progress_on_someone_elses_routine_is_routine_not_yours(make_user, api):
    """`POST routine-progress/` raised a bare PermissionDenied: `permission_denied`, in
    English."""
    client = make_user("oc_c_rp")
    theirs = _strangers_routine(make_user, "rp")
    resp = api(client).post("/api/routine/routine-progress/",
                            {"routine": theirs.pk, "day": 1, "date": TODAY().isoformat()},
                            content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")
    assert (resp.status_code, _code(resp)) == (403, "routine_not_yours"), resp.content
    assert ARABIC.search(resp.json()["error"]), resp.json()


def test_a_trainer_changing_a_clients_set_is_set_not_yours(make_user, api):
    """A trainer's PATCH or DELETE of a client's set answered `permission_denied` from
    the permission class, while their POST of one answered `set_not_yours`."""
    trainer, client, routine, rows = _program(make_user, "oc_set", coached=True)
    log = _set(_progress(client, rows[0]))
    c = api(trainer)

    edited = c.patch(f"/api/routine/set-logs/{log.pk}/", {"weight": 99}, content_type=JSON)
    assert (edited.status_code, _code(edited)) == (403, "set_not_yours"), edited.content
    removed = c.delete(f"/api/routine/set-logs/{log.pk}/")
    assert (removed.status_code, _code(removed)) == (403, "set_not_yours"), removed.content


def test_my_progress_is_clients_only(make_user, api):
    """`set-logs/my-progress/` refused a trainer with `permission_denied`; every other
    client-only route answers `clients_only`."""
    trainer = make_user("oc_t_mp", user_type="trainer")
    resp = api(trainer).get("/api/routine/set-logs/my-progress/")
    assert (resp.status_code, _code(resp)) == (403, "clients_only")


# ------------------------------------------------------------------- bulk-create
def test_bulk_create_names_every_refusal(make_user, api):
    """Eleven refusals, every one of them `bad_request`, `permission_denied` or
    `not_found`: the app could not tell a typo in the weight from a date in the
    future."""
    trainer, client, routine, rows = _program(make_user, "oc_bc", days=2)
    _with_an_empty_third_day(routine)
    theirs = _strangers_routine(make_user, "bc")
    base = {"routine_id": routine.pk, "day": 1, "date": TODAY().isoformat(),
            "reps": 8, "weight": 50}
    c = api(client)

    cases = [
        ({"date": ""}, 400, "routine_day_date_required"),
        ({"weight": "heavy"}, 400, "bad_number"),
        ({"sets": 60}, 400, "sets_out_of_range"),
        ({"weight": 5000}, 400, "weight_out_of_range"),
        ({"date": "yesterday"}, 400, "bad_date"),
        ({"date": (TODAY() + timedelta(days=1)).isoformat()}, 400, "date_in_future"),
        ({"date": (TODAY() - timedelta(days=400)).isoformat()}, 400, "date_too_old"),
        ({"routine_id": theirs.pk + 10_000}, 404, "routine_not_found"),
        ({"routine_id": theirs.pk}, 403, "routine_not_yours"),
        ({"day": 9}, 400, "day_out_of_range"),
        ({"day": 3}, 400, "day_has_no_exercises"),
    ]
    for change, status_code, code in cases:
        resp = c.post("/api/routine/set-logs/bulk-create/", {**base, **change},
                      content_type=JSON)
        assert (resp.status_code, _code(resp)) == (status_code, code), (change, resp.content)
    weight = c.post("/api/routine/set-logs/bulk-create/", {**base, "weight": "heavy"},
                    content_type=JSON)
    assert weight.json()["field"] == "weight"


@pytest.mark.parametrize("fault, code", [
    ('duplicate key value violates unique constraint "x"', "set_log_exists"),
    ("could not serialize access", "set_write_failed"),
])
def test_bulk_create_row_errors_carry_a_code(make_user, api, monkeypatch, fault, code):
    """`errors[]` rows were `{exercise, set_number, error}` only, and the duplicate
    sentence was an English literal: `bulk-complete`'s rows have carried `code` since
    S-042."""
    from django.db import IntegrityError

    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, f"oc_bce_{code}")

    def refused(*args, **kwargs):
        raise IntegrityError(fault)

    monkeypatch.setattr(ExerciseSetLog.objects, "create", refused)
    resp = api(client).post("/api/routine/set-logs/bulk-create/",
                            {"routine_id": routine.pk, "day": 1, "sets": 1, "reps": 8,
                             "date": TODAY().isoformat()},
                            content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")
    assert resp.status_code == 200, resp.content
    (error,) = resp.json()["errors"]
    assert error["code"] == code, error
    assert ARABIC.search(error["error"]), error


# ------------------------------------------------------------------ bulk-complete
def test_bulk_complete_names_every_refusal(make_user, api):
    """The same refusals as `bulk-create`, and the same generic codes."""
    trainer, client, routine, rows = _program(make_user, "oc_bk", days=2)
    _with_an_empty_third_day(routine)
    theirs = _strangers_routine(make_user, "bk")
    base = {"routine_id": routine.pk, "day": 1, "date": TODAY().isoformat()}
    c = api(client)

    cases = [
        ({"day": ""}, 400, "routine_day_date_required"),
        ({"date": "yesterday"}, 400, "bad_date"),
        ({"date": (TODAY() + timedelta(days=1)).isoformat()}, 400, "date_in_future"),
        ({"date": (TODAY() - timedelta(days=400)).isoformat()}, 400, "date_too_old"),
        ({"routine_id": theirs.pk + 10_000}, 404, "routine_not_found"),
        ({"routine_id": theirs.pk}, 403, "routine_not_yours"),
        ({"day": 9}, 400, "day_out_of_range"),
        ({"day": 3}, 400, "day_has_no_exercises"),
    ]
    for change, status_code, code in cases:
        resp = c.post("/api/routine/user-exercise-progress/bulk-complete/",
                      {**base, **change}, content_type=JSON)
        assert (resp.status_code, _code(resp)) == (status_code, code), (change, resp.content)


def test_a_date_that_does_not_exist_is_bad_date_not_a_500(make_user, api):
    """`parse_date` answers None only for a string not SHAPED like a date; "2026-02-30"
    raises ValueError. So the refusal written for a bad date was a 500 on exactly that
    typo — and on `set-logs/sync/` one such row failed the whole batch."""
    trainer, client, routine, rows = _program(make_user, "oc_nodate", coached=True)
    from tests.test_routine_templates import _template

    template, _squat, _press = _template(trainer, "oc_nodate")
    impossible = "2026-02-30"
    c = api(client)

    for path in ("/api/routine/set-logs/bulk-create/",
                 "/api/routine/user-exercise-progress/bulk-complete/"):
        resp = c.post(path, {"routine_id": routine.pk, "day": 1, "reps": 8,
                             "date": impossible}, content_type=JSON)
        assert (resp.status_code, _code(resp)) == (400, "bad_date"), (path, resp.content)

    generated = api(trainer).post(f"/api/routine/templates/{template.pk}/generate/",
                                  {"assign": False, "start_date": impossible},
                                  content_type=JSON)
    assert (generated.status_code, _code(generated)) == (400, "bad_date"), generated.content

    synced = c.post("/api/routine/set-logs/sync/", {"sets": [
        {"client_uuid": str(uuid.uuid4()), "exercise_id": rows[0].exercise_id,
         "date": impossible, "reps": 8},
        {"client_uuid": str(uuid.uuid4()), "exercise_id": rows[0].exercise_id,
         "routine_id": routine.pk, "day": 1, "reps": 8},
    ]}, content_type=JSON)
    assert synced.status_code == 200, synced.content
    first, second = synced.json()["results"]
    assert (first["status"], first["code"]) == ("error", "bad_date"), first
    assert second["status"] == "created", "one bad row must not lose the others"


def test_daily_summary_names_a_bad_date(make_user, api):
    """`?date=` that is not YYYY-MM-DD answered `bad_request`."""
    client = make_user("oc_c_ds")
    resp = api(client).get("/api/routine/user-exercise-progress/daily-summary/?date=junk")
    assert (resp.status_code, _code(resp)) == (400, "bad_date")


# ------------------------------------------------------------------ workout sessions
def test_workout_session_refusals_name_their_reason(make_user, api):
    """A trainer opening a session for a stranger raised a bare PermissionDenied; their
    PATCH raised `PermissionDenied()`; their DELETE was refused by the permission class —
    all three `permission_denied`. A client naming a routine not theirs got the field
    code `invalid`, in English."""
    trainer, client, routine, rows = _program(make_user, "oc_ws", coached=True)
    stranger_client = make_user("oc_c_ws_stranger")
    session = _session(client, routine)
    c = api(trainer)

    opened = c.post("/api/routine/workout-sessions/",
                    {"routine": routine.pk, "day": 1, "user": stranger_client.pk},
                    content_type=JSON)
    assert (opened.status_code, _code(opened)) == (403, "not_your_client"), opened.content

    finished = c.patch(f"/api/routine/workout-sessions/{session.pk}/",
                       {"status": "completed"}, content_type=JSON)
    assert (finished.status_code, _code(finished)) == (403, "session_not_yours")
    removed = c.delete(f"/api/routine/workout-sessions/{session.pk}/")
    assert (removed.status_code, _code(removed)) == (403, "session_not_yours")
    assert finished.json()["error"] == removed.json()["error"], "one meaning, one sentence"

    theirs = _strangers_routine(make_user, "ws")
    refused = api(client).post("/api/routine/workout-sessions/",
                               {"routine": theirs.pk, "day": 1}, content_type=JSON,
                               HTTP_ACCEPT_LANGUAGE="ar")
    assert refused.status_code == 400, refused.content
    (field_error,) = refused.json()["field_errors"]["routine"]
    assert field_error["code"] == "routine_not_yours", field_error
    assert ARABIC.search(field_error["message"]), field_error


# ------------------------------------------------------------------------- analytics
ANALYTICS = ("summary", "completion", "streaks", "adherence", "strength", "trends")


def test_analytics_refusals_name_their_reason(make_user, api):
    """Six actions refused somebody else's `user_id` with `permission_denied`, four
    answered a missing one with `not_found`, and two refused an unknown `period` with
    `bad_request` while `adherence` already said `bad_period`."""
    client = make_user("oc_c_an")
    other = make_user("oc_c_an_other")
    admin = make_user("oc_admin_an", user_type="admin")

    for action in ANALYTICS:
        resp = api(client).get(f"/api/routine/analytics/{action}/?user_id={other.pk}")
        assert (resp.status_code, _code(resp)) == (403, "user_not_accessible"), action

    for action in ("summary", "streaks", "adherence", "strength"):
        resp = api(admin).get(f"/api/routine/analytics/{action}/?user_id={other.pk + 10_000}")
        assert (resp.status_code, _code(resp)) == (404, "user_not_found"), action

    for action in ("summary", "trends", "adherence"):
        resp = api(client).get(f"/api/routine/analytics/{action}/?period=fortnight")
        assert (resp.status_code, _code(resp)) == (400, "bad_period"), action


# ------------------------------------------------------------------------- templates
def test_generate_names_every_refusal(make_user, api):
    """Seven refusals answered only `bad_request`, `permission_denied` or `not_found` —
    the docstring said so in as many words — while the list form already said
    `not_your_client`."""
    from tests.test_routine_templates import _template

    trainer, client, routine, rows = _program(make_user, "oc_gen", coached=True)
    other = make_user("oc_c_gen_other")
    template, _squat, _press = _template(trainer, "oc_gen")
    url = f"/api/routine/templates/{template.pk}/generate/"
    c = api(trainer)

    cases = [
        ({}, 400, "client_id_required"),
        ({"client_id": other.pk + 10_000}, 404, "client_not_found"),
        ({"client_id": other.pk}, 403, "not_your_client"),
        ({"assign": False, "start_date": "soon"}, 400, "bad_date"),
        ({"assign": False, "start_date": "2026-03-10", "end_date": "2026-03-01"}, 400,
         "end_before_start"),
        ({"assign": False, "days": "a week"}, 400, "bad_days"),
        ({"assign": False, "days": 999}, 400, "days_out_of_range"),
    ]
    for body, status_code, code in cases:
        resp = c.post(url, body, content_type=JSON)
        assert (resp.status_code, _code(resp)) == (status_code, code), (body, resp.content)
    too_long = c.post(url, {"assign": False, "days": 999}, content_type=JSON)
    assert too_long.json()["max"] == 365


# ------------------------------------------------------------------- exercise media
def test_the_image_route_names_its_refusals(make_user, api):
    """No file answered `bad_request`; removing an image the exercise does not have
    answered `not_found` — the same code as "no such exercise" — with no `error` at
    all."""
    from routine.models import Exercise

    trainer = make_user("oc_t_img", user_type="trainer")
    exercise = Exercise.objects.create(name="oc img", created_by=trainer)
    url = f"/api/routine/exercises/{exercise.pk}/image/"

    missing = api(trainer).post(url, {})
    assert (missing.status_code, _code(missing)) == (400, "image_required"), missing.content

    nothing = api(trainer).delete(url)
    assert (nothing.status_code, _code(nothing)) == (404, "no_image"), nothing.content
    assert nothing.json()["error"] == nothing.json()["message"]


def test_the_media_route_names_its_refusals(make_user, api):
    """Both empty bodies answered `bad_request`, and a partly refused DELETE carried
    sentences only: the POST had `errors_detail[]` with codes, the DELETE had none."""
    from routine.models import Exercise

    trainer = make_user("oc_t_media", user_type="trainer")
    exercise = Exercise.objects.create(name="oc media", created_by=trainer)
    url = f"/api/routine/exercises/{exercise.pk}/add-media/"
    c = api(trainer)

    added = c.post(url, json.dumps({"media_items": []}), content_type=JSON)
    assert (added.status_code, _code(added)) == (400, "media_items_required")
    removed = c.delete(url, json.dumps({"media_ids": []}), content_type=JSON)
    assert (removed.status_code, _code(removed)) == (400, "media_ids_required")

    partly = c.delete(url, json.dumps({"media_ids": [987654]}), content_type=JSON)
    assert partly.status_code == 207, partly.content
    (detail,) = partly.json()["errors_detail"]
    assert (detail["media_id"], detail["code"]) == (987654, "media_not_on_exercise")
    assert partly.json()["errors"] == [detail["message"]]


# ----------------------------------------------------------------- client progress
def test_client_progress_of_someone_elses_client_is_not_your_client(make_user, api):
    """It answered `permission_denied`, in English — the code of a client who called a
    trainer's route at all."""
    trainer = make_user("oc_t_cp", user_type="trainer")
    stranger = make_user("oc_c_cp")
    resp = api(trainer).get(f"/api/routine/trainer/client-progress/{stranger.pk}/",
                            HTTP_ACCEPT_LANGUAGE="ar")
    assert (resp.status_code, _code(resp)) == (403, "not_your_client")
    assert ARABIC.search(resp.json()["error"]), resp.json()


# ------------------------------------------------------ named in the view, not padded
def test_a_not_found_that_means_only_not_found_is_named_by_the_view(make_user):
    """Rule (b) of the decision: a 404 whose only meaning is "no such object, or not
    yours" says `not_found` itself instead of leaving it to the envelope. Asked of the
    views WITHOUT the middleware that pads a missing code, which is the only place the
    difference shows: through the middleware both answer `not_found`."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    from routine.views import AnalyticsViewSet, TrainerClientProgressViewSet, _exercise_not_found

    assert _exercise_not_found().data["code"] == "not_found"

    admin = make_user("oc_admin_nf", user_type="admin")
    client = make_user("oc_c_nf")
    factory = APIRequestFactory()

    request = factory.get(f"/api/routine/trainer/client-progress/{client.pk + 10_000}/")
    force_authenticate(request, user=admin)
    resp = TrainerClientProgressViewSet.as_view({"get": "client_progress"})(
        request, client_id=client.pk + 10_000)
    assert (resp.status_code, resp.data.get("code")) == (404, "not_found")

    request = factory.get("/api/routine/analytics/strength/?exercise_id=987654")
    force_authenticate(request, user=client)
    resp = AnalyticsViewSet.as_view({"get": "strength"})(request)
    assert (resp.status_code, resp.data.get("code")) == (404, "not_found")


# ------------------------------------------------------------------- the whole file
def _status_is_4xx(call):
    """The `status` a `Response(...)` call is built with, if it is a 4xx."""
    for keyword in call.keywords:
        if keyword.arg != "status":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return 400 <= value.value < 500
        return "HTTP_4" in ast.unparse(value)
    return False


def test_no_routine_refusal_is_left_to_the_status_default():
    """The rule itself, over the source: every 4xx `Response` routine/views.py builds
    names a `code` in its body, and every `PermissionDenied` it raises is raised WITH
    one. Seventy-three sites did neither before the decision; a new one fails here
    instead of shipping `bad_request` to the app."""
    import routine.views as views_module

    tree = ast.parse(Path(views_module.__file__).read_text(encoding="utf-8"))
    unnamed = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name == "Response" and _status_is_4xx(node):
            body = node.args[0] if node.args else None
            named = isinstance(body, ast.Dict) and any(
                isinstance(key, ast.Constant) and key.value == "code" for key in body.keys)
            if not named:
                unnamed.append(f"routine/views.py:{node.lineno} Response")
        if name.split(".")[-1] == "PermissionDenied" and not any(
                keyword.arg == "code" for keyword in node.keywords):
            unnamed.append(f"routine/views.py:{node.lineno} PermissionDenied")
    assert not unnamed, unnamed

    # The two permission classes whose refusal means "not yours", not "wrong role".
    from routine.permissions import IsSessionOwnerOrTrainerOrAdmin, IsSetLogCreatorOrTrainerOrAdmin

    assert IsSetLogCreatorOrTrainerOrAdmin.code == "set_not_yours"
    assert IsSessionOwnerOrTrainerOrAdmin.code == "session_not_yours"
