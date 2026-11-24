"""The fix run's RTN-1, S-247 (2026-11-24): an id filter is not an existence oracle. Four
viewsets still used `filterset_fields`, which builds a model-choice filter for a foreign
key and validates the id against EVERY row: an id that does not exist answered 400 and
someone else's real one an empty 200. Now both are the same empty 200 (the S-119 rule
`routine-exercises/` already had); a value that is not a whole number is still 400.

  routine-progress/?routine=&user=     set-logs/?workout_session=
  workout-sessions/?routine=&user=     template-exercises/?template=

The missing-id tests fail on the code before; the others hold what stays.
"""
import pytest
from django.utils import timezone

from tests.test_fx_rttr import _program, _progress, _session

pytestmark = pytest.mark.django_db

BASE = "/api/routine/"
MISSING = 987654321


def _ids(resp):
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return [row["id"] for row in (body["results"] if isinstance(body, dict) else body)]


@pytest.fixture
def world(make_user):
    """Two clients of two trainers, each with a routine, a session and a set; one private
    template per trainer."""
    from routine.models import ExerciseSetLog, RoutineTemplate, RoutineTemplateExercise

    mine = _program(make_user, "s247me", coached=True)
    theirs = _program(make_user, "s247them", coached=True)
    out = {}
    for key, (trainer, client, routine, rows) in (("mine", mine), ("theirs", theirs)):
        session = _session(client, routine)
        ExerciseSetLog.objects.create(user_exercise_progress=_progress(client, rows[0]),
                                      set_number=1, reps=10, weight=20,
                                      date=timezone.localdate(), workout_session=session)
        template = RoutineTemplate.objects.create(name=f"s247 {key}", goal="strength",
                                                  created_by=trainer, days=1)
        RoutineTemplateExercise.objects.create(template=template,
                                               exercise=rows[0].exercise, day=1)
        out[key] = {"trainer": trainer, "client": client, "routine": routine,
                    "session": session, "template": template}
    return out


def _queries(world):
    me, them = world["mine"], world["theirs"]
    return [
        (me["client"], "routine-progress/?routine=", me["routine"].pk, them["routine"].pk),
        (me["client"], "routine-progress/?user=", me["client"].pk, them["client"].pk),
        (me["client"], "set-logs/?workout_session=", me["session"].pk, them["session"].pk),
        (me["client"], "workout-sessions/?routine=", me["routine"].pk, them["routine"].pk),
        (me["client"], "workout-sessions/?user=", me["client"].pk, them["client"].pk),
        (me["trainer"], "template-exercises/?template=", me["template"].pk,
         them["template"].pk),
    ]


def test_a_missing_id_and_someone_elses_answer_alike(world, api):
    for who, query, _own, foreign in _queries(world):
        missing = api(who).get(f"{BASE}{query}{MISSING}")
        stranger = api(who).get(f"{BASE}{query}{foreign}")
        assert (missing.status_code, _ids(missing)) == (200, []), (query, missing.content)
        assert _ids(stranger) == [], query


def test_ones_own_id_still_filters(world, api):
    for who, query, own, _foreign in _queries(world):
        assert _ids(api(who).get(f"{BASE}{query}{own}")), query


def test_a_value_that_is_not_a_whole_number_is_still_refused(world, api):
    for who, query, _own, _foreign in _queries(world):
        for bad in ("abc", "1.5"):
            assert api(who).get(f"{BASE}{query}{bad}").status_code == 400, (query, bad)


def test_the_plain_filters_still_work(world, api):
    me = world["mine"]
    rows = _ids(api(me["client"]).get(f"{BASE}workout-sessions/?status=active"))
    assert rows == [me["session"].pk]
    today = timezone.localdate().isoformat()
    assert _ids(api(me["client"]).get(f"{BASE}set-logs/?date={today}"))
    assert _ids(api(me["client"]).get(f"{BASE}routine-progress/?status=not_started&day=1"))
