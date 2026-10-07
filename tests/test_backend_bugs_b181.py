"""BACKEND_BUGS B-181 and SIDE_BUGS S-164: the progress list, one exercise at a time.

B-181: to show «آخر مرة» beside each exercise of a day, the app asked
`user-exercise-progress/?exercise=<id>` once PER EXERCISE (six requests in the same
millisecond for a six-exercise day), then `daily-summary/` once per date for the top
set. `?latest_for=<id,id,...>&before=YYYY-MM-DD` answers the newest trained row of each
exercise before that date in one call, each with its `top_set`.

S-164: `?exercise=` was a model-choice filter over EVERY exercise, so a missing id
answered 400 "Select a valid choice" while another trainer's private exercise answered
an empty 200: exercise ids could be probed. Both are an empty page now.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

LIST = "/api/routine/user-exercise-progress/"


def _progress(user, exercise, date, completed=3, skipped=False):
    from routine.models import UserExerciseProgress

    return UserExerciseProgress.objects.create(user=user, exercise=exercise, date=date,
                                               completed_sets=completed, target_sets=3,
                                               skipped=skipped)


def _setup(make_user):
    from routine.models import Exercise

    client = make_user("b181_client")
    a = Exercise.objects.create(name="b181 A", created_by=None)
    b = Exercise.objects.create(name="b181 B", created_by=None)
    return client, a, b


def test_latest_for_answers_the_newest_trained_row_per_exercise(make_user, api):
    client, a, b = _setup(make_user)
    today = timezone.localdate()
    d = lambda n: today - timedelta(days=n)     # noqa: E731
    _progress(client, a, d(3))
    newest_a = _progress(client, a, d(1))
    newest_b = _progress(client, b, d(2))
    _progress(client, b, d(1), completed=0, skipped=True)
    _progress(client, a, today)                 # not before `before`
    _progress(make_user("b181_other"), a, d(1))  # someone else's

    resp = api(client).get(f"{LIST}?latest_for={a.pk},{b.pk}&before={today.isoformat()}")
    assert resp.status_code == 200, resp.content
    rows = resp.json()["results"]
    assert sorted((r["id"], r["date"]) for r in rows) == sorted(
        [(newest_a.pk, d(1).isoformat()), (newest_b.pk, d(2).isoformat())])


def test_latest_for_carries_the_top_set(make_user, api):
    from routine.models import ExerciseSetLog

    client, a, _b = _setup(make_user)
    day = timezone.localdate() - timedelta(days=1)
    progress = _progress(client, a, day)
    for number, (weight, reps) in enumerate([(60, 10), (80, 5), (70, 8)], start=1):
        ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=number,
                                      weight=weight, reps=reps, date=day)
    rows = api(client).get(f"{LIST}?latest_for={a.pk}").json()["results"]
    assert len(rows) == 1
    # «أفضل مجموعة» as everywhere else (the owner, F3): the highest weight x reps.
    assert rows[0]["top_set"] == {"weight": 60.0, "reps": 10, "volume_kg": 600.0}


def test_latest_for_refuses_what_is_not_a_list_of_ids(make_user, api):
    client, a, _b = _setup(make_user)
    for query in (f"latest_for={a.pk},x", "latest_for=1.5", "latest_for=", "latest_for=-3"):
        resp = api(client).get(f"{LIST}?{query}")
        assert resp.status_code == 400, (query, resp.content)
        assert (resp.json()["code"], resp.json()["field"]) == ("bad_exercise_id",
                                                               "latest_for"), resp.json()
    many = api(client).get(f"{LIST}?latest_for=" + ",".join(str(n) for n in range(1, 52)))
    assert (many.status_code, many.json()["code"], many.json()["max"]) == (
        400, "too_many_exercise_ids", 50)
    bad = api(client).get(f"{LIST}?latest_for={a.pk}&before=yesterday")
    assert (bad.status_code, bad.json()["code"], bad.json()["field"]) == (400, "bad_date",
                                                                          "before")


def test_exercise_filter_answers_a_missing_id_like_a_private_one(make_user, api):
    """S-164: the two answers are the same empty page, so nothing can be probed."""
    from routine.models import Exercise

    client, a, _b = _setup(make_user)
    rival = make_user("b181_rival", user_type="trainer")
    private = Exercise.objects.create(name="b181 private", created_by=rival)
    missing_id = Exercise.objects.order_by("-pk").values_list("pk", flat=True).first() + 1000
    _progress(client, a, timezone.localdate())

    answers = []
    for exercise_id in (private.pk, missing_id):
        resp = api(client).get(f"{LIST}?exercise={exercise_id}")
        assert resp.status_code == 200, resp.content
        answers.append(resp.json()["results"])
    assert answers == [[], []]
    mine = api(client).get(f"{LIST}?exercise={a.pk}").json()["results"]
    assert [r["exercise"]["id"] for r in mine] == [a.pk]
    assert api(client).get(f"{LIST}?exercise=abc").status_code == 400
