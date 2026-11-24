"""Road to 10 S2.1, phase 0 review round 4 (2026-11-24): the client's OWN request comes
first, however many invitations are newer.

`home/first_day.py open_request` read the ten newest pending rows and only then looked
for the client's own request among them, so a client who had asked a trainer and then
received ten or more newer invitations saw the invitation face («invited») over the
request they were waiting on («waiting»). The own request is chosen first, then the
newest invitation. Fails on the code before; the guard (invitations only) passes on both.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_road10_s21_open_request import _sections, _trainer

pytestmark = pytest.mark.django_db


def _invite(trainer, client, at):
    from users.models import TrainerClientRelation

    return TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                                status="pending", initiated_by="trainer",
                                                requested_at=at)


def test_the_clients_own_request_leads_over_eleven_newer_invitations(make_user, api):
    from home.first_day import open_request
    from users.models import TrainerClientRelation

    client = make_user("s21own_client", training_goal="build_muscle")
    now = timezone.now()
    asked = _trainer(make_user, "own_asked")
    own = TrainerClientRelation.objects.create(trainer=asked, client=client,
                                               status="pending", initiated_by="client",
                                               requested_at=now - timedelta(days=2))
    for i in range(11):
        _invite(_trainer(make_user, f"own_inv{i}"), client, now - timedelta(hours=i))

    assert open_request(client).pk == own.pk
    hero = _sections(api, client)["find_trainer_hero"]
    assert (hero["step"], hero["request_id"], hero["trainer"]["id"]) == (
        "waiting", own.pk, asked.pk)


def test_with_invitations_only_the_newest_leads(make_user, api):
    from home.first_day import open_request

    client = make_user("s21own_inv_only", training_goal="build_muscle")
    now = timezone.now()
    rows = [_invite(_trainer(make_user, f"inv_only{i}"), client, now - timedelta(hours=i))
            for i in range(11)]
    assert open_request(client).pk == rows[0].pk
    assert _sections(api, client)["find_trainer_hero"]["step"] == "invited"
