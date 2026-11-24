"""BACKEND_BUGS B-200: Home says which way a coaching request is open, whatever hero leads.

`find_trainer_hero.step` (`waiting` | `invited`, Road to 10 S2.1) is the only place Home
said which way a request was open, and that hero is left out whenever a `workout_hero`
leads: a client with no coach who bought a programme read only the checklist's
`coach.state: awaiting`, one value for a request they SENT and an invitation they
RECEIVED. The checklist's coach step now also carries `direction`, `request_id` and
`trainer {id, first_name}` from the hero's own rule (`home/first_day.py
open_request_facts`). The facts tests fail on the code before it: the step had no such
keys.

The last part answers the walk's second question: after the client withdraws a request
or declines an invitation, or the trainer declines the request, the next home read is
already fresh: the relation's save or delete retires the cached page
(`training_platform/signals.py bust_home_on_a_relation_change`, S0.6). Those tests pass
on the code before this change too; they pin the answer.
"""
from decimal import Decimal

import pytest

from tests.test_road10_s24_trial_workout import platform  # noqa: F401

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"
AUTH = "/api/auth"
JSON = "application/json"
FACTS = {"direction", "request_id", "trainer"}


@pytest.fixture(autouse=True)
def cold_cache():
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


def _page(api, person):
    resp = api(person).get(HOME, HTTP_X_APP_CHANNEL="apk")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _sections(api, person):
    from training_platform.cache import private_cache

    private_cache().clear()
    return {s["type"]: s["data"] for s in _page(api, person)["sections"]}


def _coach_step(sections):
    return next(step for step in sections["setup_checklist"]["steps"]
                if step["code"] == "coach")


def _coach(make_user, tag):
    """The trainer the client asks for, or who invites them. Named apart from the
    programme's trainer (Rami, `tests/test_routine_products.py`)."""
    return make_user(f"b200_t_{tag}", user_type="trainer", first_name="Nizar",
                     email_verified=True, trainer_is_available=True,
                     trainer_hourly_rate=Decimal("100.00"),
                     trainer_goal_focus=["build_muscle"])


def _bought_programme(make_user, api, tag):
    """A client with no coach who bought a programme: training, so `workout_hero`
    leads and `find_trainer_hero` is not sent (B-182)."""
    from tests.test_routine_products import _buy, _client, _published

    _trainer, _template, product_id = _published(make_user, api, f"b200{tag}")
    client = _client(make_user, f"b200_{tag}", training_goal="build_muscle")
    bought = _buy(api, client, product_id)
    assert bought.status_code in (200, 201), bought.content
    sections = _sections(api, client)
    assert "workout_hero" in sections and "find_trainer_hero" not in sections
    return client


def _trial_copy(make_user, api, tag):
    """A client with no coach who started the free first workout: their own copy, which
    is not a programme (`routine/trial.py`)."""
    from tests.test_coaching_months import _fund

    client = make_user(f"b200_trial_{tag}", training_goal="build_muscle")
    _fund(client, "1000.00")
    started = api(client).post("/api/routine/trial-workout/", {}, content_type=JSON)
    assert started.status_code in (200, 201), started.content
    assert _sections(api, client)["trial_workout"]["routine_id"] is not None
    return client


def _request(api, client, coach):
    from users.models import TrainerClientRelation

    sent = api(client).post(f"{AUTH}/client/request-trainer/", {"trainer_id": coach.pk},
                            content_type=JSON)
    assert sent.status_code == 200, sent.content
    return TrainerClientRelation.objects.get(trainer=coach, client=client,
                                             status="pending")


def _invite(api, client, coach):
    from users.models import TrainerClientRelation

    sent = api(coach).post(f"{AUTH}/trainer/assign-client/", {"client_id": client.pk},
                           content_type=JSON)
    assert sent.status_code in (200, 201), sent.content
    return TrainerClientRelation.objects.get(trainer=coach, client=client,
                                             status="pending", initiated_by="trainer")


def _assert_open(sections, relation, direction, coach):
    step = _coach_step(sections)
    assert step["state"] == "awaiting" and step["done"] is False
    assert (step["direction"], step["request_id"]) == (direction, relation.pk)
    assert step["trainer"] == {"id": coach.pk, "first_name": "Nizar"}
    hero = sections.get("find_trainer_hero")
    if hero is not None:
        # One rule: where the hero is sent too, it says the same.
        assert (hero["step"], hero["request_id"], hero["trainer"]) == \
            (step["direction"], step["request_id"], step["trainer"])


# ------------------------------------------------------------------ a bought programme
def test_a_bought_programme_and_a_request_sent_is_waiting(make_user, api):
    client = _bought_programme(make_user, api, "wait")
    coach = _coach(make_user, "bwait")
    relation = _request(api, client, coach)
    sections = _sections(api, client)
    assert "find_trainer_hero" not in sections          # the day's hero leads
    _assert_open(sections, relation, "waiting", coach)


def test_a_bought_programme_and_an_invitation_is_invited(make_user, api):
    client = _bought_programme(make_user, api, "inv")
    coach = _coach(make_user, "binv")
    relation = _invite(api, client, coach)
    sections = _sections(api, client)
    assert "find_trainer_hero" not in sections
    _assert_open(sections, relation, "invited", coach)


# ------------------------------------------------------------------ the free first workout
def test_a_trial_copy_and_a_request_sent_is_waiting(make_user, api, platform):  # noqa: F811
    client = _trial_copy(make_user, api, "wait")
    coach = _coach(make_user, "twait")
    relation = _request(api, client, coach)
    _assert_open(_sections(api, client), relation, "waiting", coach)


def test_a_trial_copy_and_an_invitation_is_invited(make_user, api, platform):  # noqa: F811
    client = _trial_copy(make_user, api, "inv")
    coach = _coach(make_user, "tinv")
    relation = _invite(api, client, coach)
    _assert_open(_sections(api, client), relation, "invited", coach)


# ------------------------------------------------------------------ unchanged, and private
def test_no_routine_keeps_the_hero_as_it_was(make_user, api):
    """The client with no routine: the hero's keys are the ones S2.1 shipped, and the
    checklist says the same facts beside it."""
    coach = _coach(make_user, "none")
    client = make_user("b200_none", training_goal="build_muscle")
    from tests.test_coaching_months import _fund

    _fund(client, "1000.00")
    relation = _request(api, client, coach)
    sections = _sections(api, client)
    assert set(sections["find_trainer_hero"]) == {"goal", "matches", "step",
                                                  "request_id", "trainer"}
    _assert_open(sections, relation, "waiting", coach)


def test_nothing_open_adds_nothing_to_the_coach_step(make_user, api):
    client = _bought_programme(make_user, api, "quiet")
    step = _coach_step(_sections(api, client))
    assert set(step) == {"code", "done", "state"} and step["state"] != "awaiting"


def test_a_blocked_trainers_invitation_says_nothing_and_no_name(make_user, api):
    from moderation.models import Block

    client = _bought_programme(make_user, api, "blk")
    coach = _coach(make_user, "bblk")
    from training_platform.cache import private_cache

    _invite(api, client, coach)
    Block.objects.create(blocker=client, blocked=coach)
    private_cache().clear()
    body = _page(api, client)
    step = _coach_step({s["type"]: s["data"] for s in body["sections"]})
    assert step["state"] != "awaiting" and not FACTS & set(step)
    assert "Nizar" not in str(body)


def test_a_suspended_trainers_request_says_nothing(make_user, api):
    client = _bought_programme(make_user, api, "susp")
    coach = _coach(make_user, "bsusp")
    _request(api, client, coach)
    coach.is_active = False
    coach.save(update_fields=["is_active"])
    step = _coach_step(_sections(api, client))
    assert step["state"] != "awaiting" and not FACTS & set(step)


# ------------------------------------------------------------------ the cache (S0.6)
def _awaiting_then_cached(api, client):
    """The page as the app last read it, saying `awaiting`, and proof it is served from
    the cache (the same `as_of` twice). Cleared first: outside a captured commit the
    setup's own writes retired nothing, so an entry from before the request may stand."""
    from training_platform.cache import private_cache

    private_cache().clear()
    first = _page(api, client)
    assert _page(api, client)["as_of"] == first["as_of"], "the page is not cached"
    sections = {s["type"]: s["data"] for s in first["sections"]}
    assert _coach_step(sections)["state"] == "awaiting"
    return first


def _after(api, client, first):
    """The next read, WITHOUT clearing the cache: what the app is served."""
    body = _page(api, client)
    sections = {s["type"]: s["data"] for s in body["sections"]}
    assert body["as_of"] != first["as_of"], "served the page from before the action"
    step = _coach_step(sections)
    assert step["state"] != "awaiting" and not FACTS & set(step)


def test_a_withdrawn_request_retires_the_cached_home(make_user, api,
                                                     django_capture_on_commit_callbacks):
    client = _bought_programme(make_user, api, "cxl")
    _request(api, client, _coach(make_user, "bcxl"))
    first = _awaiting_then_cached(api, client)
    with django_capture_on_commit_callbacks(execute=True):
        done = api(client).post(f"{AUTH}/client/cancel-trainer-request/", {},
                                content_type=JSON)
    assert done.status_code == 200, done.content
    _after(api, client, first)


def test_a_declined_invitation_retires_the_cached_home(make_user, api,
                                                       django_capture_on_commit_callbacks):
    client = _bought_programme(make_user, api, "dec")
    relation = _invite(api, client, _coach(make_user, "bdec"))
    first = _awaiting_then_cached(api, client)
    with django_capture_on_commit_callbacks(execute=True):
        done = api(client).post(f"{AUTH}/client/respond-to-invite/",
                                {"request_id": relation.pk, "action": "reject"},
                                content_type=JSON)
    assert done.status_code == 200, done.content
    _after(api, client, first)


def test_a_request_the_trainer_declines_retires_the_cached_home(
        make_user, api, django_capture_on_commit_callbacks):
    """The trainer's side of a request. An invitation has no trainer's side to test:
    no route lets a trainer withdraw one (`trainer/unassign-client/` acts on an
    approved coaching only)."""
    client = _bought_programme(make_user, api, "trj")
    coach = _coach(make_user, "btrj")
    relation = _request(api, client, coach)
    first = _awaiting_then_cached(api, client)
    with django_capture_on_commit_callbacks(execute=True):
        done = api(coach).post(f"{AUTH}/trainer/respond-to-request/",
                               {"request_id": relation.pk, "action": "reject"},
                               content_type=JSON)
    assert done.status_code == 200, done.content
    _after(api, client, first)
