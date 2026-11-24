"""The fix run's RTN-1, G1 (2026-11-24): a trainer retires their own exercise. `DELETE`
answers 409 `exercise_has_history` (and now `exercise_in_use`) telling them to set
`is_active=false`, but `is_active` was not a field of `ExerciseSerializer`: only the
/dj-admin/ console could set it, and a PATCH carrying it was silently ignored.

Now readable and writable (additive) by the exercise's author or an admin. The first two
tests fail on the code before; the others hold what stays.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _name(tag):
    return f"g1_{tag}_{uuid.uuid4().hex[:6]}"


def _exercise(trainer):
    from routine.models import Exercise

    return Exercise.objects.create(name=_name("ex"), created_by=trainer)


def test_the_author_retires_and_restores_an_exercise(make_user, api):
    from routine.models import Exercise

    trainer = make_user(_name("t"), user_type="trainer")
    move = _exercise(trainer)
    url = f"/api/routine/exercises/{move.pk}/"

    retired = api(trainer).patch(url, {"is_active": False}, content_type=JSON)
    assert retired.status_code == 200, retired.content
    assert retired.json()["is_active"] is False
    assert Exercise.objects.get(pk=move.pk).is_active is False

    back = api(trainer).patch(url, {"is_active": True}, content_type=JSON)
    assert (back.status_code, back.json()["is_active"]) == (200, True)


def test_every_read_says_whether_it_is_retired(make_user, api):
    from routine.models import Exercise

    trainer = make_user(_name("t2"), user_type="trainer")
    live = _exercise(trainer)
    old = Exercise.objects.create(name=_name("old"), created_by=trainer, is_active=False)
    one = api(trainer).get(f"/api/routine/exercises/{old.pk}/").json()
    assert one["is_active"] is False
    rows = api(trainer).get("/api/routine/exercises/").json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    flags = {row["id"]: row["is_active"] for row in rows}
    assert flags[live.pk] is True and flags[old.pk] is False


def test_nobody_else_retires_it(make_user, api):
    from routine.models import Exercise

    author = make_user(_name("t3"), user_type="trainer")
    other = make_user(_name("t4"), user_type="trainer")
    move = _exercise(author)
    refused = api(other).patch(f"/api/routine/exercises/{move.pk}/", {"is_active": False},
                               content_type=JSON)
    assert refused.status_code in (403, 404), refused.content
    assert Exercise.objects.get(pk=move.pk).is_active is True


def test_a_retired_exercise_cannot_join_a_routine_but_stays_in_one(make_user, api):
    from routine.models import Routine, RoutineExercise

    trainer = make_user(_name("t5"), user_type="trainer")
    held, fresh = _exercise(trainer), _exercise(trainer)
    routine = Routine.objects.create(name=_name("r"), created_by=trainer, days=1)
    row = RoutineExercise.objects.create(routine=routine, exercise=held, day=1, sets=3,
                                         reps=10)
    for move in (held, fresh):
        assert api(trainer).patch(f"/api/routine/exercises/{move.pk}/",
                                  {"is_active": False},
                                  content_type=JSON).status_code == 200
    assert RoutineExercise.objects.filter(pk=row.pk).exists()
    added = api(trainer).post("/api/routine/routine-exercises/",
                              {"routine": routine.pk, "exercise_id": fresh.pk, "day": 1,
                               "order": 2, "sets": 3, "reps": 10}, content_type=JSON)
    assert added.status_code == 400, added.content
    assert added.json()["field_errors"]["exercise"][0]["code"] == "exercise_retired"
