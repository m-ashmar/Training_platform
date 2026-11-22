"""Road to 10 S2.10 (the owner's D16, 2026-11-22): starter templates for trainers, written
by the platform.

`public_templates/` and `copy/` existed, but no public template did, and any trainer
could mark their own template public. Now the platform writes a handful of public
templates — one per goal of the closed list and per place, in Arabic and English — that
every trainer lists and copies into their own library, and a trainer's own template
cannot be made public (its own `code`). These fail before the item.
"""
import json

import pytest

pytestmark = pytest.mark.django_db

TEMPLATES = "/api/routine/templates/"
PUBLIC = "/api/routine/templates/public_templates/"
JSON = "application/json"
GOALS = {"build_muscle", "strength", "fat_loss", "rehab"}
ARABIC = "ابتثجحخدذرزسشصضطظعغفقكلمنهوي"


@pytest.fixture
def starters(db):
    """What `bootstrap_platform` gives a new server: the starter exercises and the
    platform's templates (`routine/platform.py`)."""
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.data.platform_templates import PLATFORM_TEMPLATES
    from routine.models import Exercise
    from routine.platform import ensure_platform_templates

    wanted = {row.exercise for t in PLATFORM_TEMPLATES for row in t.rows}
    for entry in STARTER_EXERCISES:
        if entry.key in wanted and not Exercise.objects.filter(
                catalogue_key=entry.key).exists():
            Exercise(name=entry.name_en, name_en=entry.name_en, name_ar=entry.name_ar,
                     target_muscle=entry.target_muscle, equipment=entry.equipment,
                     difficulty_level=entry.difficulty, created_by=None, is_active=True,
                     catalogue_key=entry.key).save()
    report = ensure_platform_templates()
    assert not report["missing_exercises"], report
    return report


def _rows(resp):
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body["results"] if isinstance(body, dict) else body


def _platform(api, person, **query):
    params = "&".join(f"{k}={v}" for k, v in {"by_platform": "true", "page_size": 100,
                                               **query}.items())
    return _rows(api(person).get(f"{PUBLIC}?{params}"))


# ---------------------------------------------------------------- what is there
def test_every_trainer_lists_the_platforms_starters_across_goals_and_places(
        make_user, api, starters):
    trainer = make_user("s210_lister", user_type="trainer")
    rows = _platform(api, trainer)
    assert {(row["goal"], row["place"]) for row in rows} == {
        (goal, place) for goal in GOALS for place in ("home", "gym")}
    assert len(rows) == 8
    for row in rows:
        assert row["by_platform"] is True and row["created_by"] is None, row
        assert row["is_public"] is True
        assert row["days"] == 3 and row["weeks"] and row["session_count"] == 3 * row["weeks"]
        assert {r["day"] for r in row["exercises"]} == {1, 2, 3}, row["name"]
        assert row["used_by_count"] is None


def test_they_are_named_in_arabic_and_in_english(make_user, api, starters):
    trainer = make_user("s210_lang", user_type="trainer")
    english = {row["id"]: row for row in _platform(api, trainer)}
    arabic = {row["id"]: row for row in _rows(api(trainer).get(
        f"{PUBLIC}?by_platform=true&page_size=100", HTTP_ACCEPT_LANGUAGE="ar"))}
    assert set(english) == set(arabic)
    for pk, row in english.items():
        assert row["name"] and not any(ch in ARABIC for ch in row["name"]), row["name"]
        assert any(ch in ARABIC for ch in arabic[pk]["name"]), arabic[pk]["name"]
        assert any(ch in ARABIC for ch in arabic[pk]["description"])


def test_a_home_starter_needs_nothing_a_home_does_not_have(make_user, api, starters):
    """`home` means the body and a pair of dumbbells (the catalogue's own `home` mark)."""
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.models import Exercise

    at_home = {e.key for e in STARTER_EXERCISES if e.home}
    trainer = make_user("s210_home", user_type="trainer")
    for row in _platform(api, trainer, place="home"):
        keys = set(Exercise.objects.filter(
            pk__in=[r["exercise"]["id"] for r in row["exercises"]])
            .values_list("catalogue_key", flat=True))
        assert keys <= at_home, (row["name"], keys - at_home)
    assert len(_platform(api, trainer, place="gym")) == 4
    assert {row["goal"] for row in _platform(api, trainer, goal="rehab")} == {"rehab"}


def test_a_client_reads_them_too_and_the_trial_workouts_are_not_among_them(
        make_user, api, starters):
    client = make_user("s210_client")
    rows = _platform(api, client)
    assert len(rows) == 8
    assert not [row for row in rows if row["name"].startswith("Full body")]


# ---------------------------------------------------------------- copying one
def test_a_trainer_copies_a_starter_into_their_own_library(make_user, api, starters):
    from routine.models import RoutineTemplate

    trainer = make_user("s210_copier", user_type="trainer")
    starter = next(row for row in _platform(api, trainer, place="gym")
                   if row["goal"] == "strength")
    resp = api(trainer).post(f"{TEMPLATES}{starter['id']}/copy/", content_type=JSON)
    assert resp.status_code == 201, resp.content
    copy = resp.json()
    assert (copy["name"], copy["goal"], copy["place"], copy["days"], copy["weeks"]) == (
        starter["name"], "strength", "gym", 3, starter["weeks"])
    assert (copy["is_public"], copy["by_platform"]) == (False, False)
    assert len(copy["exercises"]) == len(starter["exercises"])
    stored = RoutineTemplate.objects.get(pk=copy["id"])
    assert stored.created_by_id == trainer.pk and stored.platform_key is None
    assert stored.name_ar == RoutineTemplate.objects.get(pk=starter["id"]).name_ar
    # Theirs now: in their own list, and the starter is unchanged for everyone else.
    mine = _rows(api(trainer).get(f"{TEMPLATES}my_templates/"))
    assert copy["id"] in [row["id"] for row in mine]
    assert len(_platform(api, trainer)) == 8


def test_a_trainers_copy_of_their_own_still_says_copy_of(make_user, api):
    from routine.models import RoutineTemplate

    trainer = make_user("s210_own", user_type="trainer")
    own = RoutineTemplate.objects.create(name_en="Mine", name_ar="قالبي", goal="strength",
                                         days=1, created_by=trainer, place="home")
    copy = api(trainer).post(f"{TEMPLATES}{own.pk}/copy/", content_type=JSON).json()
    assert copy["name"] == "Copy of Mine" and copy["place"] == "home"


# ---------------------------------------------------------------- nobody else's is public
def test_a_trainers_own_template_cannot_be_made_public(make_user, api):
    from routine.models import RoutineTemplate

    trainer = make_user("s210_public", user_type="trainer")
    body = {"name": "Public attempt", "goal": "strength", "days": 1, "exercises": [],
            "is_public": True}
    resp = api(trainer).post(TEMPLATES, data=json.dumps(body), content_type=JSON)
    assert resp.status_code == 400, resp.content
    assert (resp.json()["code"], resp.json()["field"]) == (
        "template_cannot_be_public", "is_public")
    assert not RoutineTemplate.objects.filter(created_by=trainer).exists()

    body["is_public"] = False
    created = api(trainer).post(TEMPLATES, data=json.dumps(body), content_type=JSON)
    assert created.status_code == 201, created.content
    pk = created.json()["id"]
    for method in ("patch", "put"):
        resp = getattr(api(trainer), method)(f"{TEMPLATES}{pk}/", data=json.dumps(
            {**body, "is_public": "true"}), content_type=JSON)
        assert resp.status_code == 400, (method, resp.content)
        assert resp.json()["code"] == "template_cannot_be_public"
    assert RoutineTemplate.objects.get(pk=pk).is_public is False
    # Sending false, or leaving it out, is an ordinary edit.
    ok = api(trainer).patch(f"{TEMPLATES}{pk}/", data=json.dumps(
        {"name": "Renamed", "is_public": False}), content_type=JSON)
    assert ok.status_code == 200, ok.content


def test_place_is_written_as_a_code_and_anything_else_is_refused(make_user, api):
    trainer = make_user("s210_place", user_type="trainer")
    body = {"name": "Placed", "goal": "fat_loss", "days": 1, "exercises": [],
            "place": " Gym "}
    resp = api(trainer).post(TEMPLATES, data=json.dumps(body), content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert resp.json()["place"] == "gym"
    body["place"] = "park"
    bad = api(trainer).post(TEMPLATES, data=json.dumps(body), content_type=JSON)
    assert bad.status_code == 400, bad.content
    assert bad.json()["field_errors"]["place"][0]["code"] == "bad_place"


# ---------------------------------------------------------------- a new server
def test_bootstrap_writes_the_starters_once(db):
    from io import StringIO

    from django.core.management import call_command

    from routine.models import RoutineTemplate

    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=StringIO())
    starters = RoutineTemplate.objects.filter(platform_key__startswith="starter-")
    assert starters.count() == 8
    assert all(t.created_by_id is None and t.is_public and t.place in ("home", "gym")
               for t in starters)
    assert {t.goal for t in starters} == GOALS
    before = list(starters.order_by("pk").values_list("pk", "updated_at"))
    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=StringIO())
    assert list(RoutineTemplate.objects.filter(platform_key__startswith="starter-")
                .order_by("pk").values_list("pk", "updated_at")) == before
