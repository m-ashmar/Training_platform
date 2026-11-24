"""The fix run's RTN-1, S-166 (2026-11-24): an admin creates an exercise. Both create
routes let an admin in ("a trainer or an admin", S-117) and then saved the admin as the
owner, which `Exercise.clean` refuses (`trainer_required`: only a trainer owns an
exercise) — `create-with-image/` answered 500 `exercise_create_failed`.

An admin's exercise is now the platform catalogue's (no owner, `is_global`), exactly what
the console makes; a trainer's stays their own. The admin tests fail on the code before.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

CREATE = "/api/routine/exercises/create-with-image/"
EXERCISES = "/api/routine/exercises/"
JSON = "application/json"


def _name(tag):
    return f"s166_{tag}_{uuid.uuid4().hex[:6]}"


def _form(name):
    return {"name": name, "description": "d", "target_muscle": "Biceps"}


def test_an_admins_form_exercise_joins_the_catalogue(make_user, api):
    from routine.models import Exercise

    admin = make_user(_name("a"), user_type="admin")
    name = _name("ex")
    resp = api(admin).post(CREATE, _form(name))
    assert resp.status_code == 201, resp.content
    made = Exercise.objects.get(name=name)
    assert (made.created_by_id, made.is_global) == (None, True)


def test_an_admins_plain_exercise_joins_the_catalogue_and_everyone_sees_it(make_user, api):
    from routine.models import Exercise

    admin = make_user(_name("a2"), user_type="admin")
    trainer = make_user(_name("t"), user_type="trainer")
    name = _name("ex2")
    resp = api(admin).post(EXERCISES, {"name": name, "target_muscle": "Biceps"},
                           content_type=JSON)
    assert resp.status_code == 201, resp.content
    made = Exercise.objects.get(name=name)
    assert (made.created_by_id, made.is_global) == (None, True)
    seen = api(trainer).get(f"{EXERCISES}{made.pk}/")
    assert seen.status_code == 200 and seen.json()["is_mine"] is False


def test_a_trainers_exercise_is_still_their_own(make_user, api):
    from routine.models import Exercise

    trainer = make_user(_name("t2"), user_type="trainer")
    name = _name("ex3")
    assert api(trainer).post(CREATE, _form(name)).status_code == 201
    made = Exercise.objects.get(name=name)
    assert (made.created_by_id, made.is_global) == (trainer.pk, False)
