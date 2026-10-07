"""BACKEND_BUGS B-139: the refusals of a routine row write carried no code.

`RoutineExerciseSerializer.validate` raised `ValidationError({field: sentence})` for
another trainer's routine, a retired exercise and a day past the routine's `days`, so
`field_errors.<field>[0].code` was DRF's default `invalid` for all of them, and the app
could not tell one from another. Another trainer's PRIVATE exercise answered its own
sentence while a missing id answered `does_not_exist`: an existence oracle on exercise
ids (the S-056 defect, on this route).

The template path had the sibling: another trainer's exercise answered an English,
code-less sentence nested inside the row, which never reached `field_errors` at all.
"""
import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
ROWS = "/api/routine/routine-exercises/"
TEMPLATES = "/api/routine/templates/"


def _first(resp, field):
    body = resp.json()
    assert resp.status_code == 400, resp.content
    assert body["code"] == "validation_error", body
    return body["field_errors"][field][0]


def _setup(make_user):
    from routine.models import Exercise, Routine

    trainer = make_user("b139_trainer", user_type="trainer")
    rival = make_user("b139_rival", user_type="trainer")
    mine = Routine.objects.create(name="b139 mine", created_by=trainer, days=3)
    theirs = Routine.objects.create(name="b139 theirs", created_by=rival, days=3)
    own = Exercise.objects.create(name="b139 own", created_by=trainer)
    retired = Exercise.objects.create(name="b139 retired", created_by=None, is_active=False)
    private = Exercise.objects.create(name="b139 private", created_by=rival)
    return trainer, rival, mine, theirs, own, retired, private


def test_routine_exercise_refusals_name_their_codes(make_user, api):
    from routine.models import Exercise

    trainer, _rival, mine, theirs, own, retired, private = _setup(make_user)
    client = api(trainer)

    def post(**body):
        row = {"routine": mine.pk, "exercise_id": own.pk, "day": 1, "order": 1,
               "sets": 3, "reps": 10}
        row.update(body)
        return client.post(ROWS, row, content_type=JSON)

    assert _first(post(exercise_id=retired.pk), "exercise")["code"] == "exercise_retired"
    assert _first(post(day=9), "day")["code"] == "day_out_of_range"
    assert _first(post(routine=theirs.pk), "routine")["code"] == "not_routine_owner"

    missing_id = (Exercise.objects.order_by("-pk").values_list("pk", flat=True).first()
                  or 0) + 1000
    missing = _first(post(exercise_id=missing_id), "exercise_id")
    foreign = _first(post(exercise_id=private.pk), "exercise_id")
    assert missing["code"] == foreign["code"] == "does_not_exist"
    assert foreign["message"] == missing["message"].replace(str(missing_id),
                                                            str(private.pk))

    assert post().status_code == 201


def test_a_day_past_the_routine_is_refused_by_code_on_patch_too(make_user, api):
    from routine.models import RoutineExercise

    trainer, _rival, mine, _theirs, own, _retired, _private = _setup(make_user)
    row = RoutineExercise.objects.create(routine=mine, exercise=own, day=1, order=1)
    resp = api(trainer).patch(f"{ROWS}{row.pk}/", {"day": 4}, content_type=JSON)
    assert _first(resp, "day")["code"] == "day_out_of_range"


def test_a_template_row_with_another_trainers_exercise_answers_does_not_exist(make_user,
                                                                              api):
    from routine.models import Exercise

    trainer, _rival, _mine, _theirs, own, _retired, private = _setup(make_user)
    missing_id = (Exercise.objects.order_by("-pk").values_list("pk", flat=True).first()
                  or 0) + 1000

    def post(exercise_id):
        return api(trainer).post(TEMPLATES, {
            "name": "b139 tpl", "goal": "g", "days": 2,
            "exercises": [{"exercise_id": own.pk, "day": 1, "order": 1},
                          {"exercise_id": exercise_id, "day": 2, "order": 1}]},
            content_type=JSON)

    foreign = _first(post(private.pk), "exercises")
    missing = _first(post(missing_id), "exercises")
    assert foreign["code"] == missing["code"] == "does_not_exist"
    assert foreign["message"] == missing["message"].replace(str(missing_id),
                                                            str(private.pk))
    assert post(own.pk).status_code == 201
