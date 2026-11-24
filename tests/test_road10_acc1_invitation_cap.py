"""The fix run's ACC-1, S-201 (2026-11-24): a trainer's 25 outstanding invitations do
not count one nobody can answer — to a client who blocked the trainer, or whom the
trainer blocked. A block touches no relation row, and the client does not see the
invitation while it stands, so it filled the 25 for good (the client's side of the same
row was fixed by ca234f8). The first test fails on the code before; the second holds the
cap itself.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
ASSIGN = "/api/auth/trainer/assign-client/"


def _name(tag):
    return f"s201_{tag}_{uuid.uuid4().hex[:6]}"


def _invited(make_user, trainer, n):
    from users.models import TrainerClientRelation

    clients = [make_user(_name(f"c{i}")) for i in range(n)]
    for client in clients:
        TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                             status="pending", initiated_by="trainer")
    return clients


def test_invitations_nobody_can_answer_do_not_fill_the_cap(make_user, api):
    from moderation.models import Block

    trainer = make_user(_name("t"), user_type="trainer")
    clients = _invited(make_user, trainer, 25)
    Block.objects.create(blocker=clients[0], blocked=trainer)     # the client blocked
    Block.objects.create(blocker=trainer, blocked=clients[1])     # the trainer blocked

    newcomer = make_user(_name("new"))
    sent = api(trainer).post(ASSIGN, {"client_id": newcomer.pk}, content_type=JSON)
    assert sent.status_code == 200, sent.content


def test_twenty_five_answerable_invitations_still_fill_it(make_user, api):
    trainer = make_user(_name("t2"), user_type="trainer")
    _invited(make_user, trainer, 25)
    refused = api(trainer).post(ASSIGN, {"client_id": make_user(_name("new2")).pk},
                                content_type=JSON)
    assert refused.status_code == 429
    assert refused.json()["code"] == "too_many_invitations"
