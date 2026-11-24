"""The fix run's RTN-1, G2 (2026-11-24): an exercise a routine or a template still uses is
not deleted. `RoutineExercise` and `RoutineTemplateExercise` CASCADE, so a delete with no
logged work behind it took the exercise out of every routine that used it — a client's
assigned programme included — and out of every template, silently. Now 409
`exercise_in_use` (additive) with how many routines and templates name it. The in-use
tests fail on the code before; the others hold what stays.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db


def _name(tag):
    return f"g2_{tag}_{uuid.uuid4().hex[:6]}"


def _exercise(trainer):
    from routine.models import Exercise

    return Exercise.objects.create(name=_name("ex"), created_by=trainer)


def test_an_exercise_in_a_clients_routine_is_not_deleted(make_user, api):
    from routine.models import Exercise, Routine, RoutineExercise

    trainer, client = make_user(_name("t"), user_type="trainer"), make_user(_name("c"))
    move = _exercise(trainer)
    routine = Routine.objects.create(name=_name("r"), created_by=trainer, days=1)
    row = RoutineExercise.objects.create(routine=routine, exercise=move, day=1, sets=3,
                                         reps=10)
    routine.assigned_to.add(client)

    refused = api(trainer).delete(f"/api/routine/exercises/{move.pk}/")
    assert refused.status_code == 409, refused.content
    body = refused.json()
    assert (body["code"], body["routines"], body["templates"]) == ("exercise_in_use", 1, 0)
    assert Exercise.objects.filter(pk=move.pk).exists()
    assert RoutineExercise.objects.filter(pk=row.pk).exists()


def test_an_exercise_in_a_template_is_not_deleted(make_user, api):
    from routine.models import RoutineTemplate, RoutineTemplateExercise

    trainer = make_user(_name("t2"), user_type="trainer")
    move = _exercise(trainer)
    template = RoutineTemplate.objects.create(name=_name("tpl"), goal="strength",
                                              created_by=trainer, days=1)
    RoutineTemplateExercise.objects.create(template=template, exercise=move, day=1)

    refused = api(trainer).delete(f"/api/routine/exercises/{move.pk}/")
    assert (refused.status_code, refused.json()["code"]) == (409, "exercise_in_use")
    assert refused.json()["templates"] == 1


def test_logged_work_still_answers_its_own_code_first(make_user, api):
    from routine.models import Routine, RoutineExercise, UserExerciseProgress

    trainer, client = make_user(_name("t3"), user_type="trainer"), make_user(_name("c3"))
    move = _exercise(trainer)
    routine = Routine.objects.create(name=_name("r3"), created_by=trainer, days=1)
    RoutineExercise.objects.create(routine=routine, exercise=move, day=1, sets=3, reps=10)
    UserExerciseProgress.objects.create(user=client, exercise=move)
    refused = api(trainer).delete(f"/api/routine/exercises/{move.pk}/")
    assert refused.json()["code"] == "exercise_has_history"


def test_an_unused_exercise_is_still_deleted(make_user, api):
    trainer = make_user(_name("t4"), user_type="trainer")
    move = _exercise(trainer)
    assert api(trainer).delete(f"/api/routine/exercises/{move.pk}/").status_code == 204
