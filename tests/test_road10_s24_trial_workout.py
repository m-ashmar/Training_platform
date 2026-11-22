"""Road to 10 S2.4 (the owner's D10, 2026-11-22): a free first workout, from the server.

`SECTION_ORDER` had no trial section and the card existed in the app's mock data only, so
a new account had nothing to DO on its first day. Now a client with no coach and no
programme is offered the platform's ~20-minute no-equipment workout for the place they
gave at onboarding; starting it writes THEIR OWN copy (a routine is one person's private
copy), which is logged through the ordinary workout routes and kept in history, without
a streak or a programme they would later lose. Free on every build, sells nothing.
These fail before the item: the section, the route and the platform rows did not exist.
"""
import json

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"
URL = "/api/routine/trial-workout/"
JSON = "application/json"
BUILDS = ("apk", "play", "ios", "web", "")


@pytest.fixture
def platform(db):
    """What `bootstrap_platform` gives a new server: the starter exercises the trial
    workouts are made of, and the platform's templates (`routine/platform.py`)."""
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.data.platform_templates import TRIAL_WORKOUTS
    from routine.models import Exercise
    from routine.platform import ensure_platform_templates

    wanted = {row.exercise for t in TRIAL_WORKOUTS for row in t.rows}
    for entry in STARTER_EXERCISES:
        if entry.key in wanted and not Exercise.objects.filter(
                catalogue_key=entry.key).exists():
            Exercise(name=entry.name_en, name_en=entry.name_en, name_ar=entry.name_ar,
                     target_muscle=entry.target_muscle, equipment=entry.equipment,
                     difficulty_level=entry.difficulty, created_by=None, is_active=True,
                     catalogue_key=entry.key).save()
    # The trial workouts alone: since S2.10 the platform also writes starter templates,
    # made of exercises this fixture does not create, and they are not this file's.
    report = ensure_platform_templates(TRIAL_WORKOUTS)
    assert not report["missing_exercises"], report
    _bust()
    return report


def _bust():
    from training_platform.cache import private_cache

    private_cache().clear()


def _home(api, person, **extra):
    resp = api(person).get(HOME, **extra)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _types(body):
    return [section["type"] for section in body["sections"]]


def _section(body, kind):
    found = [s["data"] for s in body["sections"] if s["type"] == kind]
    return found[0] if found else None


def _client(make_user, tag, place=""):
    return make_user(f"s24_{tag}", training_place=place, training_goal="fat_loss")


def _approved(make_user, client, tag):
    from users.models import TrainerClientRelation

    trainer = make_user(f"s24_coach_{tag}", user_type="trainer", first_name="Rami")
    return TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                                status="approved")


def _session(api, person, routine_id, **extra):
    resp = api(person).post("/api/routine/workout-sessions/",
                            data=json.dumps({"routine": routine_id, "day": 1, **extra}),
                            content_type=JSON)
    assert resp.status_code == 201, resp.content
    return resp.json()["id"]


# ---------------------------------------------------------------- what is offered
def test_a_new_client_is_offered_the_home_workout_in_the_first_day_list(
        make_user, api, platform):
    from home.views import SECTION_ORDER

    person = _client(make_user, "fresh")
    body = _home(api, person, HTTP_X_APP_CHANNEL="apk")
    types = _types(body)
    assert "trial_workout" in types, types
    assert types == sorted(types, key=SECTION_ORDER.index)
    assert SECTION_ORDER.index("featured_trainers") < SECTION_ORDER.index(
        "trial_workout") < SECTION_ORDER.index("ready_programs")

    data = _section(body, "trial_workout")
    assert set(data) == {"template_id", "routine_id", "day", "place", "name",
                         "description", "exercises", "sets", "estimated_minutes",
                         "equipment", "no_equipment", "muscles", "last_done_on",
                         "times_done"}
    assert (data["routine_id"], data["day"], data["place"]) == (None, 1, "home")
    assert (data["exercises"], data["sets"]) == (5, 11)
    assert 18 <= data["estimated_minutes"] <= 22, data["estimated_minutes"]
    assert (data["equipment"], data["no_equipment"]) == (["bodyweight"], True)
    assert data["muscles"][0] == "Front Quads"
    assert (data["last_done_on"], data["times_done"]) == (None, 0)
    # The route answers the same object.
    assert api(person).get(URL).json() == data


@pytest.mark.parametrize("place, key", [("home", "trial-home"), ("gym", "trial-gym"),
                                        ("", "trial-home")])
def test_the_workout_is_the_one_for_the_place_given_at_onboarding(
        make_user, api, platform, place, key):
    from routine.models import RoutineTemplate

    person = _client(make_user, f"place_{place or 'none'}", place=place)
    data = api(person).get(URL).json()
    assert data["template_id"] == RoutineTemplate.objects.get(platform_key=key).pk
    assert data["place"] == (place or "home")
    assert data["no_equipment"] is True


def test_it_is_free_and_identical_on_every_build(make_user, api, platform):
    person = _client(make_user, "builds")
    answers = []
    for channel in BUILDS:
        _bust()
        data = _section(_home(api, person, HTTP_X_APP_CHANNEL=channel), "trial_workout")
        assert data is not None, channel
        answers.append(data)
        route = api(person).get(URL, HTTP_X_APP_CHANNEL=channel)
        assert route.status_code == 200 and route.json() == data, channel
    assert all(answer == answers[0] for answer in answers)
    flat = json.dumps(answers[0])
    for priced in ("price", "currency", "money_type", "can_buy"):
        assert priced not in flat


# ---------------------------------------------------------------- who is not offered
def test_a_client_with_a_coach_is_not_offered_it_frozen_or_not(make_user, api, platform):
    person = _client(make_user, "coached")
    _approved(make_user, person, "coached")
    assert "trial_workout" not in _types(_home(api, person))
    resp = api(person).get(URL)
    assert resp.status_code == 404, resp.content
    assert (resp.json()["code"], resp.json()["reason"]) == ("trial_not_offered", "has_coach")
    assert api(person).post(URL).status_code == 404


def test_a_client_with_a_programme_is_not_offered_it(make_user, api, platform):
    """A routine assigned to them is a programme — a bought one, a coach's, a former
    coach's frozen one — and the freeze rule holds: the trial is no way round it."""
    from routine.models import Routine

    person = _client(make_user, "programme")
    trainer = make_user("s24_writer", user_type="trainer")
    routine = Routine.objects.create(name="Theirs", created_by=trainer, days=3)
    routine.assigned_to.add(person)
    _bust()
    assert "trial_workout" not in _types(_home(api, person))
    resp = api(person).post(URL)
    assert resp.status_code == 404, resp.content
    assert (resp.json()["code"], resp.json()["reason"]) == ("trial_not_offered", "has_program")
    assert not Routine.objects.filter(created_by=person).exists()


def test_a_trainer_is_refused_and_a_server_without_the_workouts_offers_nothing(
        make_user, api):
    trainer = make_user("s24_trainer", user_type="trainer")
    resp = api(trainer).get(URL)
    assert (resp.status_code, resp.json()["code"]) == (403, "not_a_client")

    person = _client(make_user, "bare")          # no `platform` fixture: nothing written
    assert "trial_workout" not in _types(_home(api, person))
    resp = api(person).post(URL)
    assert resp.status_code == 404, resp.content
    assert resp.json()["reason"] == "unavailable"
    assert api().get(URL).status_code == 401


# ---------------------------------------------------------------- starting it
def test_starting_writes_the_persons_own_copy_once(make_user, api, platform):
    from routine.models import Routine, RoutineTemplate

    person = _client(make_user, "start", place="gym")
    other = _client(make_user, "start_other", place="gym")
    first = api(person).post(URL)
    assert first.status_code == 201, first.content
    again = api(person).post(URL)
    assert again.status_code == 200, again.content
    assert first.json()["routine_id"] == again.json()["routine_id"] is not None

    copy = Routine.objects.get(pk=first.json()["routine_id"])
    template = RoutineTemplate.objects.get(platform_key="trial-gym")
    assert (copy.trial, copy.created_by_id, copy.days, copy.source_template_id) == (
        True, person.pk, 1, template.pk)
    assert not copy.assigned_to.exists(), "a trial is never a programme"
    assert (sorted(copy.routine_exercises.values_list("exercise_id", "sets", "reps"))
            == sorted(template.routinetemplateexercise_set.values_list(
                "exercise_id", "sets", "reps")))
    assert Routine.objects.filter(created_by=person, trial=True).count() == 1

    # A private copy: the next person gets a routine of their own.
    theirs = api(other).post(URL).json()["routine_id"]
    assert theirs != copy.pk
    # The home section now names the copy.
    _bust()
    assert _section(_home(api, person), "trial_workout")["routine_id"] == copy.pk


def test_the_copy_is_read_by_id_but_is_not_a_programme(make_user, api, platform):
    person = _client(make_user, "read")
    routine_id = api(person).post(URL).json()["routine_id"]

    detail = api(person).get(f"/api/routine/routines/{routine_id}/")
    assert detail.status_code == 200, detail.content
    assert detail.json()["trial"] is True
    assert len(detail.json()["routine_exercises"]) == 5
    listed = api(person).get("/api/routine/routines/").json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert routine_id not in [row["id"] for row in rows]
    assert api(person).get(f"/api/routine/routines/{routine_id}/schedule/").status_code == 404
    # Nobody else reads it.
    stranger = _client(make_user, "read_other")
    assert api(stranger).get(f"/api/routine/routines/{routine_id}/").status_code == 404
    # Home stays the first-day home: no hero for a routine that is not a programme.
    _bust()
    types = _types(_home(api, person))
    assert "workout_hero" not in types and "trial_workout" in types, types


def test_it_is_trained_through_the_ordinary_routes_and_kept_in_history(
        make_user, api, platform, django_capture_on_commit_callbacks):
    from routine.models import DayOutcome, UserExerciseProgress, WorkoutSession

    person = _client(make_user, "train")
    routine_id = api(person).post(URL).json()["routine_id"]
    session_id = _session(api, person, routine_id)
    assert api(person).get(URL).json()["session_id"] == session_id

    from routine.models import Routine

    today = timezone.localdate()
    for row in Routine.objects.get(pk=routine_id).routine_exercises.all():
        progress, _new = UserExerciseProgress.objects.get_or_create(
            user=person, exercise=row.exercise, date=today,
            defaults={"target_sets": row.sets})
        for number in range(1, row.sets + 1):
            resp = api(person).post("/api/routine/set-logs/", data=json.dumps(
                {"user_exercise_progress": progress.pk, "set_number": number,
                 "reps": row.reps, "date": today.isoformat(),
                 "workout_session": session_id}), content_type=JSON)
            assert resp.status_code in (200, 201), resp.content
    with django_capture_on_commit_callbacks(execute=True):
        done = api(person).patch(f"/api/routine/workout-sessions/{session_id}/",
                                 data=json.dumps({"status": "completed"}),
                                 content_type=JSON)
    assert done.status_code == 200, done.content

    outcome = DayOutcome.objects.get(user=person, routine_id=routine_id, day=1, date=today)
    assert outcome.complete, (outcome.planned_sets, outcome.logged_sets)
    history = api(person).get("/api/routine/workout-sessions/").json()["results"]
    assert session_id in [row["id"] for row in history]
    assert WorkoutSession.objects.get(pk=session_id).status == "completed"

    data = api(person).get(URL).json()
    assert (data["last_done_on"], data["times_done"]) == (today.isoformat(), 1)
    assert "session_id" not in data

    # Without a streak: a free first workout is not a training day.
    from routine.outcomes import complete_dates, training_dates

    assert today not in complete_dates(person)
    assert today not in training_dates(person)[0]
    _bust()
    body = _home(api, person)
    assert "trial_workout" in _types(body)
    week = _section(body, "week_stats")
    assert week is None or week["streak"]["current"] == 0, week


def test_no_coaching_ever_freezes_the_copy_and_the_offer_goes_when_a_coach_comes(
        make_user, api, platform):
    from users.coaching import training_refusal
    from routine.models import Routine

    person = _client(make_user, "freeze")
    routine_id = api(person).post(URL).json()["routine_id"]
    assert training_refusal(person, Routine.objects.get(pk=routine_id)) is None
    session_id = _session(api, person, routine_id)

    _approved(make_user, person, "freeze")
    _bust()
    assert "trial_workout" not in _types(_home(api, person))
    assert api(person).get(URL).json()["reason"] == "has_coach"
    # A session already running may still be finished.
    done = api(person).patch(f"/api/routine/workout-sessions/{session_id}/",
                             data=json.dumps({"status": "completed"}), content_type=JSON)
    assert done.status_code == 200, done.content


def test_the_platform_workouts_are_not_templates_anyone_can_list_or_copy(
        make_user, api, platform):
    from routine.models import RoutineTemplate

    trainer = make_user("s24_lister", user_type="trainer")
    trial_ids = set(RoutineTemplate.objects.filter(
        platform_key__in=("trial-home", "trial-gym")).values_list("pk", flat=True))
    listed = api(trainer).get("/api/routine/templates/public_templates/").json()["results"]
    assert not trial_ids & {row["id"] for row in listed}
    mine = api(trainer).get("/api/routine/templates/").json()["results"]
    assert not trial_ids & {row["id"] for row in mine}
    some = next(iter(trial_ids))
    assert api(trainer).post(f"/api/routine/templates/{some}/copy/").status_code == 404


# ---------------------------------------------------------------- a new server
def test_bootstrap_writes_the_two_workouts_once_and_leaves_the_operators_edit(db):
    from io import StringIO

    from django.core.management import call_command

    from routine.models import RoutineTemplate

    out = StringIO()
    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=out)
    assert "platform templates" in out.getvalue()
    home = RoutineTemplate.objects.get(platform_key="trial-home")
    gym = RoutineTemplate.objects.get(platform_key="trial-gym")
    for template in (home, gym):
        assert template.created_by_id is None and template.is_public is False
        assert template.name_ar and template.name_en
        assert template.routinetemplateexercise_set.count() == 5
        assert set(template.routinetemplateexercise_set.values_list(
            "exercise__equipment", flat=True)) == {"bodyweight"}

    home.name_en = "Operator's own name"
    home.save()
    again = StringIO()
    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=again)
    home.refresh_from_db()
    assert home.name_en == "Operator's own name"
    assert RoutineTemplate.objects.filter(platform_key__startswith="trial-").count() == 2
