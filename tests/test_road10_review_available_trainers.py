"""Road to 10 S0.3, phase 0 review (2026-11-23; the manager's yes the same day):
`GET /api/auth/client/available-trainers/` lists by the directory's rule.

The signed-in twin of `trainers/public/`. It chose trainers by its own filter, so a
trainer with no first name was still offered to a client after S0.3 took them off the
directory, the matches and home, and it gave the raw `client_count` below the owner's
privacy rule (B-154: a count shown from 5 up). The first two fail on the code before the
fix; the block guard passes on both.
"""
import pytest

pytestmark = pytest.mark.django_db

AVAILABLE = "/api/auth/client/available-trainers/?page_size=100"


def _trainer(make_user, tag, *, first_name="سارة"):
    trainer = make_user(tag, user_type="trainer")
    type(trainer).objects.filter(pk=trainer.pk).update(
        first_name=first_name, email_verified=True, trainer_is_available=True,
        trainer_hourly_rate=20000)
    trainer.refresh_from_db()
    return trainer


def _rows(api, client):
    resp = api(client).get(AVAILABLE, HTTP_X_APP_CHANNEL="apk")
    assert resp.status_code == 200, resp.content
    return {row["id"]: row for row in resp.json()["results"]["available_trainers"]}


def test_a_nameless_trainer_is_not_offered_to_a_client(make_user, api):
    client = make_user("avail_client")
    named = _trainer(make_user, "avail_named")
    nameless = _trainer(make_user, "avail_nameless", first_name="")
    blank = _trainer(make_user, "avail_blank", first_name="  ")
    rows = _rows(api, client)
    assert named.pk in rows
    assert nameless.pk not in rows and blank.pk not in rows


def test_a_trainers_client_count_is_shown_from_five(make_user, api):
    client = make_user("avail_counter")
    few, many = _trainer(make_user, "avail_few"), _trainer(make_user, "avail_many")
    for trainer, n in ((few, 2), (many, 5)):
        for i in range(n):
            other = make_user(f"{trainer.username}_c{i}")
            type(other).objects.filter(pk=other.pk).update(assigned_trainer=trainer)
    rows = _rows(api, client)
    assert rows[few.pk]["client_count"] is None
    assert rows[many.pk]["client_count"] == 5


def test_a_blocked_trainer_is_still_not_offered(make_user, api):
    from moderation.models import Block

    client = make_user("avail_blocker")
    trainer = _trainer(make_user, "avail_blocked")
    Block.objects.create(blocker=client, blocked=trainer)
    assert trainer.pk not in _rows(api, client)
