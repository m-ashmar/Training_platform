"""BACKEND_BUGS B-162: the CreateChallenge artboard's «من يستطيع الانضمام: عملائي فقط /
كل المجتمع» had no field — every challenge was open to the whole community.

The owner's decision (2026-09-28): `audience` = `community` (default) / `my_clients`,
settable only by a trainer creator (field code `audience_trainers_only`). A
`my_clients` challenge is HIDDEN from non-clients (list and detail) and closed to their
join (403 `challenge_clients_only`); a FROZEN client still counts; a client whose
coaching ends mid-challenge STAYS in the standing. "Client" is the approved relation,
never `assigned_trainer`.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def a_cold_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _client_of(trainer, client, *, frozen=False):
    """An approved relation, as accepting a coaching writes one; `frozen`: its paid
    month ended and the renewal could not be paid (`users/coaching.py` `frozen_at`)."""
    from users.models import CoachingPeriod, TrainerClientRelation

    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="approved", initiated_by="client",
        responded_at=timezone.now() - timedelta(days=40))
    if frozen:
        ended = timezone.now() - timedelta(days=2)
        CoachingPeriod.objects.create(
            relation=relation, trainer=trainer, client=client, number=1,
            price=Decimal("100.00"), commission_rate=Decimal("0.2000"),
            status="settled", hold_reference=str(uuid.uuid4()),
            starts_at=ended - timedelta(days=30), ends_at=ended, frozen_at=ended)
    return relation


def _create(api, creator, **body):
    now = timezone.now()
    payload = {"title": "b162 challenge", "description": "for my people",
               "challenge_type": "strength",
               "start_date": (now - timedelta(days=1)).isoformat(),
               "end_date": (now + timedelta(days=10)).isoformat(),
               "target_value": 10, "unit": "sets", "metric": "sets"}
    payload.update(body)
    return api(creator).post("/api/social/challenges/", payload, content_type=JSON)


def _listed(api, person):
    body = api(person).get("/api/social/challenges/").json()
    rows = body["results"] if isinstance(body, dict) else body
    return {row["id"] for row in rows}


def test_b162_clients_only_challenge_refuses_a_stranger(make_user, api):
    from users.coaching import coaching_status

    trainer = make_user("b162_t", user_type="trainer", trainer_is_verified=True)
    client = make_user("b162_c")
    frozen = make_user("b162_frozen")
    stranger = make_user("b162_s")
    _client_of(trainer, client)
    frozen_relation = _client_of(trainer, frozen, frozen=True)
    assert coaching_status(frozen_relation)["status"] == "frozen"

    made = _create(api, trainer, audience="my_clients")
    assert made.status_code == 201, made.content
    assert made.json()["audience"] == "my_clients"
    challenge_id = made.json()["id"]

    for member in (client, frozen):
        assert challenge_id in _listed(api, member)
        joined = api(member).post(f"/api/social/challenges/{challenge_id}/join/", {},
                                  content_type=JSON)
        assert joined.status_code == 201, joined.content

    refused = api(stranger).post(f"/api/social/challenges/{challenge_id}/join/", {},
                                 content_type=JSON)
    assert refused.status_code == 403, refused.content
    assert refused.json()["code"] == "challenge_clients_only"
    # Hidden from the stranger: not listed, and every other route answers 404.
    assert challenge_id not in _listed(api, stranger)
    for path in (f"/api/social/challenges/{challenge_id}/",
                 f"/api/social/challenges/{challenge_id}/leaderboard/"):
        assert api(stranger).get(path).status_code == 404, path
    assert challenge_id in _listed(api, trainer)

    # A community challenge is everyone's, as before.
    open_one = _create(api, trainer, title="b162 open")
    assert open_one.status_code == 201, open_one.content
    assert open_one.json()["audience"] == "community"
    # The stranger's list from above is cached for 120 s, and another person's create
    # reaching it is SIDE_BUGS S-194 (its own test): this one asks afresh.
    from django.core.cache import cache

    cache.clear()
    assert open_one.json()["id"] in _listed(api, stranger)


def test_b162_only_a_trainer_creator_may_choose_my_clients(make_user, api):
    member = make_user("b162m_c")
    trainer = make_user("b162m_t", user_type="trainer")

    refused = _create(api, member, audience="my_clients")
    assert refused.status_code == 400, refused.content
    assert refused.json()["field_errors"]["audience"][0]["code"] == \
        "audience_trainers_only"
    assert _create(api, member, audience="community").status_code == 201

    # A member's own challenge cannot be switched to it either.
    mine = _create(api, member, title="b162 mine").json()["id"]
    switched = api(member).patch(f"/api/social/challenges/{mine}/",
                                 {"audience": "my_clients"}, content_type=JSON)
    assert switched.status_code == 400, switched.content
    assert switched.json()["field_errors"]["audience"][0]["code"] == \
        "audience_trainers_only"

    theirs = _create(api, trainer, title="b162 theirs").json()["id"]
    closed = api(trainer).patch(f"/api/social/challenges/{theirs}/",
                                {"audience": "my_clients"}, content_type=JSON)
    assert closed.status_code == 200, closed.content
    assert closed.json()["audience"] == "my_clients"


def test_b162_a_client_whose_coaching_ends_stays_in_the_standing(make_user, api):
    from django.db import transaction

    from users.coaching import end_coaching

    trainer = make_user("b162e_t", user_type="trainer", trainer_is_verified=True)
    client = make_user("b162e_c")
    relation = _client_of(trainer, client)
    challenge_id = _create(api, trainer, audience="my_clients").json()["id"]
    joined = api(client).post(f"/api/social/challenges/{challenge_id}/join/", {},
                              content_type=JSON)
    assert joined.status_code == 201, joined.content

    with transaction.atomic():
        end_coaching(relation, reason="finished")

    # No longer a client, still a participant: listed, readable, on the board.
    assert challenge_id in _listed(api, client)
    detail = api(client).get(f"/api/social/challenges/{challenge_id}/")
    assert detail.status_code == 200, detail.content
    assert detail.json()["is_joined"] is True
    board = api(client).get(f"/api/social/challenges/{challenge_id}/leaderboard/")
    assert board.status_code == 200, board.content
    assert board.json()["me"] is not None
    again = api(client).post(f"/api/social/challenges/{challenge_id}/join/", {},
                             content_type=JSON)
    assert again.status_code == 200, again.content
