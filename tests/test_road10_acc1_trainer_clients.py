"""The fix run's ACC-1, G10 (2026-11-24): `GET /api/auth/trainer/clients/` lists the
clients of an APPROVED relation, and holds a client's address and body record back while
the coaching may not be used.

It listed `request.user.clients` — the reverse of the `assigned_trainer` pointer — with
each client's email, height, weight and preferences, and asked nothing of the coaching.
So a client whose pointer still named a dismissed trainer was shown to that trainer, and
a frozen coaching's client record was read as if the coaching were running. Every other
read of a client's record goes through the approved relation
(`routine.permissions.accessible_user_ids`) and, for the record itself, the coaching's
state (`diet/trainer_reads.py`). Each test fails on the code before.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

CLIENTS = "/api/auth/trainer/clients/"
BODY = ("email", "height", "weight", "age", "gender", "activity_level", "client_goals",
        "client_preferences")


def _name(tag):
    return f"g10_{tag}_{uuid.uuid4().hex[:6]}"


def _ids(api, trainer):
    resp = api(trainer).get(CLIENTS)
    assert resp.status_code == 200, resp.content
    return [row["id"] for row in resp.json()["clients"]]


def test_the_list_is_the_approved_relation_never_the_pointer(make_user, api):
    from users.models import CustomUser, TrainerClientRelation

    trainer = make_user(_name("t"), user_type="trainer")
    other = make_user(_name("t2"), user_type="trainer")
    coached = make_user(_name("coached"))
    TrainerClientRelation.objects.create(trainer=trainer, client=coached, status="approved",
                                         initiated_by="client")
    # Its pointer drifted to another trainer: still this trainer's client.
    CustomUser.objects.filter(pk=coached.pk).update(assigned_trainer=other)
    # A pointer at this trainer with no approved relation: dismissed, pending, or never.
    dismissed = make_user(_name("dismissed"))
    TrainerClientRelation.objects.create(trainer=trainer, client=dismissed,
                                         status="rejected", initiated_by="client")
    pending = make_user(_name("pending"))
    TrainerClientRelation.objects.create(trainer=trainer, client=pending, status="pending",
                                         initiated_by="client")
    stranger = make_user(_name("stranger"))
    CustomUser.objects.filter(pk__in=[dismissed.pk, pending.pk, stranger.pk]).update(
        assigned_trainer=trainer)

    assert _ids(api, trainer) == [coached.pk]
    assert _ids(api, other) == []
    body = api(trainer).get(CLIENTS).json()
    assert body["client_count"] == 1
    assert body["clients"][0]["coaching_refusal"] is None
    assert body["clients"][0]["email"] == coached.email

    trainer.refresh_from_db()
    assert list(trainer.get_accessible_clients().values_list("pk", flat=True)) == [coached.pk]


def test_a_frozen_coachings_record_is_held_back_and_the_client_still_listed(
        make_user, api, settings):
    from tests.test_coaching_freeze import _coached, _freeze

    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)
    trainer, client, relation, *_rest = _coached(make_user, api, _name("fz"))
    type(client).objects.filter(pk=client.pk).update(weight=81.5, height=178,
                                                     client_goals="strength")

    (row,) = api(trainer).get(CLIENTS).json()["clients"]
    assert row["coaching_refusal"] is None and row["weight"] == 81.5

    _freeze(relation)
    (row,) = api(trainer).get(CLIENTS).json()["clients"]
    assert row["id"] == client.pk and row["username"] == client.username
    assert row["coaching_refusal"] == "coaching_frozen"
    for key in BODY:
        assert key in row and row[key] is None, (key, row[key])
