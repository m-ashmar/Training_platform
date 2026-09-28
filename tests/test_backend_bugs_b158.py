"""BACKEND_BUGS B-158: the exercise list took no filter.

`ExerciseViewSet` declared no filter backend, so `?target_muscle=` was silently ignored
and «بدائل من مكتبة رامي» read the whole readable library (up to 300 rows) to find six
alternatives. `?target_muscle=` narrows the rows the caller may already read, never
widens them; a value that is not a muscle answers 400 `invalid_choice` on the field.
`?exclude=<id>` leaves out the exercise being replaced.
"""
import pytest

pytestmark = pytest.mark.django_db

LIST = "/api/routine/exercises/"


def _all(client, query):
    rows, url = [], f"{LIST}?{query}&page_size=100"
    while url:
        resp = client.get(url)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        rows += body["results"]
        url = body.get("next")
    return rows


def test_exercise_list_filters_by_target_muscle(make_user, api):
    from routine.models import Exercise
    from routine.permissions import visible_exercises_for
    from users.models import TrainerClientRelation

    trainer = make_user("b158_trainer", user_type="trainer")
    stranger = make_user("b158_stranger", user_type="trainer")
    client = make_user("b158_client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    curl = Exercise.objects.create(name="b158 curl", created_by=trainer,
                                   target_muscle="Biceps")
    hammer = Exercise.objects.create(name="b158 hammer", created_by=trainer,
                                     target_muscle="Biceps")
    Exercise.objects.create(name="b158 pushdown", created_by=trainer, target_muscle="Triceps")
    hidden = Exercise.objects.create(name="b158 hidden", created_by=stranger,
                                     target_muscle="Biceps")
    Exercise.objects.create(name="b158 catalogue curl", created_by=None,
                            target_muscle="Biceps")

    rows = _all(api(client), "target_muscle=Biceps")
    ids = {r["id"] for r in rows}
    assert rows and {r["target_muscle"] for r in rows} == {"Biceps"}
    assert {curl.pk, hammer.pk} <= ids
    assert hidden.pk not in ids
    visible = set(visible_exercises_for(client).filter(target_muscle="Biceps")
                  .values_list("pk", flat=True))
    assert ids == visible

    without = {r["id"] for r in _all(api(client), f"target_muscle=Biceps&exclude={curl.pk}")}
    assert without == ids - {curl.pk}

    bad = api(client).get(f"{LIST}?target_muscle=Nope")
    assert bad.status_code == 400, bad.content
    assert bad.json()["field_errors"]["target_muscle"][0]["code"] == "invalid_choice"
    worse = api(client).get(f"{LIST}?exclude=abc")
    assert worse.status_code == 400, worse.content
