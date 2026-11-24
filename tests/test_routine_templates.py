"""A trainer's template becomes each client's own private routine (the owner, 2026-09-19).

A trainer saves a template (exercises, sets, reps), personalises it per client and
assigns it; every client gets a separate copy, and the template stays reusable. See
routine/assignment.py.
"""
from decimal import Decimal

import pytest

from tests.test_coaching_months import OWNER_RATE, _accepted, _balance

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def owners_rate(settings):
    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30


def _template(trainer, tag, *, days=2, public=False):
    from routine.models import Exercise, RoutineTemplate, RoutineTemplateExercise

    template = RoutineTemplate.objects.create(name_en=f"Strength {tag}", name_ar=f"قوة {tag}",
                                              goal="strength", days=days, created_by=trainer,
                                              is_public=public)
    squat = Exercise.objects.create(name=f"rt squat {tag}", created_by=trainer)
    press = Exercise.objects.create(name=f"rt press {tag}", created_by=trainer)
    RoutineTemplateExercise.objects.create(template=template, exercise=squat, day=1, order=1,
                                           sets=5, reps=5)
    RoutineTemplateExercise.objects.create(template=template, exercise=press, day=2, order=1,
                                           sets=3, reps=10)
    return template, squat, press


def _generate(api, trainer, template, **body):
    return api(trainer).post(f"/api/routine/templates/{template.pk}/generate/", body,
                             content_type=JSON)


def test_a_first_program_from_a_template_starts_the_paid_month(make_user, api):
    """generate never settled month 1: the coaching read awaiting_first_program for
    ever, the trainer was never paid and the monthly renewal never began."""
    trainer, client, relation = _accepted(make_user, api, "rt_pay")
    template, _squat, _press = _template(trainer, "pay")

    resp = _generate(api, trainer, template, client_id=client.pk)
    assert resp.status_code == 201, resp.content
    (month,) = relation.periods.all()
    assert month.status == "settled"
    assert _balance(trainer) == Decimal("80.00")
    status = api(client).get("/api/v1/coaching/current/").json()["coaching"]["status"]
    assert status == "active"


def test_every_client_gets_a_private_copy_and_the_template_stays_reusable(make_user, api):
    from routine.models import Routine, RoutineTemplateExercise

    trainer, first, _r1 = _accepted(make_user, api, "rt_many_a")
    second = make_user("cm_c_rt_many_b")
    from tests.test_coaching_months import _fund
    _fund(second, "1000.00")
    from users.models import TrainerClientRelation
    api(second).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                     content_type=JSON)
    rel = TrainerClientRelation.objects.get(trainer=trainer, client=second)
    api(trainer).post("/api/auth/trainer/respond-to-request/",
                      {"request_id": rel.pk, "action": "approve"}, content_type=JSON)
    template, squat, _press = _template(trainer, "many")

    resp = _generate(api, trainer, template, client_ids=[first.pk, second.pk],
                     customizations={str(squat.pk): {"reps": 8}})
    assert resp.status_code == 201, resp.content
    ids = [r["id"] for r in resp.json()["routines"]]
    assert len(set(ids)) == 2
    for routine_id, client in zip(ids, (first, second)):
        routine = Routine.objects.get(pk=routine_id)
        assert list(routine.assigned_to.all()) == [client]
        assert routine.source_template_id == template.pk
        assert routine.routine_exercises.get(exercise=squat).reps == 8

    # Editing the template afterwards changes no one's routine.
    RoutineTemplateExercise.objects.filter(template=template, exercise=squat).update(reps=1)
    assert Routine.objects.get(pk=ids[0]).routine_exercises.get(exercise=squat).reps == 8
    # And the trainer can list the copies of one template.
    listed = api(trainer).get(f"/api/routine/routines/?source_template={template.pk}").json()
    assert {r["id"] for r in listed["results"]} == set(ids)

    # One client personalised by hand, the other untouched: nothing is shared.
    one = Routine.objects.get(pk=ids[0]).routine_exercises.get(exercise=squat)
    patched = api(trainer).patch(f"/api/routine/routine-exercises/{one.pk}/", {"sets": 2},
                                 content_type=JSON)
    assert patched.status_code == 200, patched.content
    assert Routine.objects.get(pk=ids[1]).routine_exercises.get(exercise=squat).sets == 5


def test_a_draft_is_personalised_then_assigned(make_user, api):
    """assign=false writes the copy for no one; assign_to_client then assigns and pays."""
    from routine.models import Routine

    trainer, client, relation = _accepted(make_user, api, "rt_draft")
    template, _squat, press = _template(trainer, "draft")

    resp = _generate(api, trainer, template, assign=False, name="Sam's block")
    assert resp.status_code == 201, resp.content
    draft = Routine.objects.get(pk=resp.json()["id"])
    assert draft.assigned_to.count() == 0 and draft.name == "Sam's block"
    assert relation.periods.get().status == "held", "a draft paid the trainer"

    row = draft.routine_exercises.get(exercise=press)
    api(trainer).patch(f"/api/routine/routine-exercises/{row.pk}/", {"reps": 6, "weight": 40},
                       content_type=JSON)
    assigned = api(trainer).post(f"/api/routine/routines/{draft.pk}/assign_to_client/",
                                 {"client_id": client.pk}, content_type=JSON)
    assert assigned.status_code == 200, assigned.content
    assert relation.periods.get().status == "settled"
    row.refresh_from_db()
    assert (row.reps, row.weight) == (6, 40)


def test_the_copy_carries_both_languages(make_user, api):
    from routine.models import Routine

    trainer, client, _relation = _accepted(make_user, api, "rt_lang")
    template, _s, _p = _template(trainer, "lang")
    resp = _generate(api, trainer, template, client_id=client.pk)
    routine = Routine.objects.get(pk=resp.json()["id"])
    assert routine.name == "Strength lang"
    assert routine.translations["ar"]["name"] == "قوة lang"
    shown = api(client).get(f"/api/routine/routines/{routine.pk}/",
                            HTTP_ACCEPT_LANGUAGE="ar").json()
    assert shown["name"] == "قوة lang"


@pytest.mark.parametrize("custom", [
    {"SQUAT": {"sets": "abc"}},
    {"SQUAT": {"sets": -3}},
    {"SQUAT": 5},
    {"SQUAT": {"reps": 0}},
    {"SQUAT": {"weight": "heavy"}},
    "not an object",
])
def test_a_bad_customisation_is_refused_not_a_500(make_user, api, custom):
    from routine.models import Routine

    trainer, client, _relation = _accepted(make_user, api, "rt_bad")
    template, squat, _press = _template(trainer, "bad")
    if isinstance(custom, dict):
        custom = {str(squat.pk): v for v in custom.values()}
    before = Routine.objects.count()
    resp = _generate(api, trainer, template, client_id=client.pk, customizations=custom)
    assert (resp.status_code, resp.json()["code"]) == (400, "bad_customization"), resp.content
    assert Routine.objects.count() == before


def test_a_trainer_cannot_generate_for_themselves_or_a_stranger(make_user, api):
    trainer, client, _relation = _accepted(make_user, api, "rt_self")
    template, _s, _p = _template(trainer, "self")
    stranger = make_user("rt_stranger")

    assert _generate(api, trainer, template, client_id=trainer.pk).status_code == 404
    assert _generate(api, trainer, template, client_id=stranger.pk).status_code == 403
    listed = _generate(api, trainer, template, client_ids=[client.pk, stranger.pk])
    assert (listed.status_code, listed.json()["code"]) == (403, "not_your_client")
    assert listed.json()["client_ids"] == [stranger.pk]
    bad = _generate(api, trainer, template, client_ids="1,2")
    assert (bad.status_code, bad.json()["code"]) == (400, "invalid_client_ids")


def test_a_template_refuses_rows_no_routine_can_hold(make_user, api):
    from routine.models import Exercise

    trainer = make_user("rt_rows_t", user_type="trainer")
    ok = Exercise.objects.create(name="rt rows ok", created_by=trainer)
    retired = Exercise.objects.create(name="rt rows old", created_by=trainer, is_active=False)
    url = "/api/routine/templates/"

    def post(rows, days=2):
        # Shared setup, not an assertion: the goal is a closed list since 2026-09-28
        # (owner; `RoutineTemplateSerializer.validate_goal`), and "g" is not one of it.
        return api(trainer).post(url, {"name": "Rows", "goal": "strength", "days": days,
                                       "exercises": rows}, content_type=JSON)

    def code(resp):
        return resp.json()["field_errors"]["exercises"][0]["code"]

    beyond = post([{"exercise_id": ok.pk, "day": 3, "order": 1}])
    assert (beyond.status_code, code(beyond)) == (400, "template_day_out_of_range")
    twice = post([{"exercise_id": ok.pk, "day": 1, "order": 1},
                  {"exercise_id": ok.pk, "day": 1, "order": 1}])
    assert (twice.status_code, code(twice)) == (400, "template_duplicate_row")
    old = post([{"exercise_id": retired.pk, "day": 1, "order": 1}])
    assert (old.status_code, code(old)) == (400, "exercise_retired")
    good = post([{"exercise_id": ok.pk, "day": 2, "order": 1}])
    assert good.status_code == 201, good.content

    shrink = api(trainer).patch(f"{url}{good.json()['id']}/", {"days": 1}, content_type=JSON)
    assert (shrink.status_code, code(shrink)) == (400, "template_day_out_of_range")


def test_an_empty_template_is_not_a_program(make_user, api):
    from routine.models import RoutineTemplate

    trainer, client, relation = _accepted(make_user, api, "rt_empty")
    # A code: the model takes only the closed goal list (wave-1 review R-5, 2026-10-07).
    empty = RoutineTemplate.objects.create(name="Empty", goal="rehab", days=1,
                                           created_by=trainer)
    resp = _generate(api, trainer, empty, client_id=client.pk)
    assert (resp.status_code, resp.json()["code"]) == (400, "template_empty")
    assert relation.periods.get().status == "held"


def test_a_trainer_can_copy_their_own_template(make_user, api):
    trainer = make_user("rt_copy_t", user_type="trainer")
    template, _s, _p = _template(trainer, "copy")
    resp = api(trainer).post(f"/api/routine/templates/{template.pk}/copy/", content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert len(resp.json()["exercises"]) == 2 and resp.json()["is_public"] is False

    other = make_user("rt_copy_other", user_type="trainer")
    assert api(other).post(f"/api/routine/templates/{template.pk}/copy/",
                           content_type=JSON).status_code in (400, 404)


def test_a_routine_someone_trained_on_is_not_handed_to_the_next_client(make_user, api):
    """After an unassign the old client's sessions stay on the routine; handing it on
    put them in the next client's 'private' program."""
    from routine.models import Routine, WorkoutSession

    trainer, first, _r = _accepted(make_user, api, "rt_hand_a")
    # G5 (2026-11-24): an empty routine is refused before this test's question
    # (routine_empty), so the handed-on routine has an exercise.
    from tests.routine_rows import deliverable_routine
    routine = deliverable_routine(trainer, "Handed", days=1)
    routine.assigned_to.add(first)
    WorkoutSession.objects.create(user=first, routine=routine, day=1)
    routine.assigned_to.remove(first)

    second = make_user("cm_c_rt_hand_b")
    from users.models import TrainerClientRelation
    TrainerClientRelation.objects.create(trainer=trainer, client=second, status="approved")
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                             {"client_id": second.pk}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (409, "routine_belongs_to_another_client")


def test_a_trainer_trying_out_a_draft_can_still_assign_it(make_user, api):
    from routine.models import WorkoutSession

    trainer, client, relation = _accepted(make_user, api, "rt_try")
    template, _s, _p = _template(trainer, "try")
    draft = _generate(api, trainer, template, assign=False).json()
    WorkoutSession.objects.create(user=trainer, routine_id=draft["id"], day=1)
    resp = api(trainer).post(f"/api/routine/routines/{draft['id']}/assign_to_client/",
                             {"client_id": client.pk}, content_type=JSON)
    assert resp.status_code == 200, resp.content


def test_a_renamed_copy_reads_the_new_name_in_every_language(make_user, api):
    trainer, client, _relation = _accepted(make_user, api, "rt_rename")
    template, _s, _p = _template(trainer, "rename")
    routine_id = _generate(api, trainer, template, client_id=client.pk).json()["id"]
    renamed = api(trainer).patch(f"/api/routine/routines/{routine_id}/", {"name": "Sam block"},
                                 content_type=JSON)
    assert renamed.status_code == 200, renamed.content
    shown = api(client).get(f"/api/routine/routines/{routine_id}/", HTTP_ACCEPT_LANGUAGE="ar").json()
    assert shown["name"] == "Sam block"


def test_many_clients_times_many_days_is_refused(make_user, api):
    from users.models import TrainerClientRelation

    trainer, first, _r = _accepted(make_user, api, "rt_big_a")
    more = [make_user(f"cm_c_rt_big_{n}") for n in ("b", "c")]
    for client in more:
        TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    template, _s, _p = _template(trainer, "big")
    resp = _generate(api, trainer, template, client_ids=[first.pk] + [c.pk for c in more],
                     days=365)
    assert (resp.status_code, resp.json()["code"]) == (400, "generate_too_large")


def test_the_console_saves_legacy_routines_and_pays_like_the_api(make_user, api):
    from types import SimpleNamespace

    from django.contrib.admin.sites import site

    from routine.admin import OneClientRoutineForm, RoutineAdmin
    from routine.models import Routine, RoutineExercise

    trainer, client, relation = _accepted(make_user, api, "rt_console")
    other = make_user("rt_console_other")
    shared = Routine.objects.create(name="legacy", created_by=trainer, days=1)
    shared.assigned_to.add(client, other)       # from before one-client-per-routine
    form = OneClientRoutineForm(instance=shared)
    form.cleaned_data = {"assigned_to": type(client).objects.filter(pk=client.pk)}
    assert list(form.clean_assigned_to()) == [client], "un-sharing must save"

    fresh = Routine.objects.create(name="console", created_by=trainer, days=1)
    template, squat, _p = _template(trainer, "console")
    RoutineExercise.objects.create(routine=fresh, exercise=squat, day=1, sets=3, reps=5)
    fake = SimpleNamespace(instance=fresh, save_m2m=lambda: fresh.assigned_to.add(client))
    RoutineAdmin(Routine, site).save_related(None, fake, [], True)
    assert relation.periods.get().status == "settled"

