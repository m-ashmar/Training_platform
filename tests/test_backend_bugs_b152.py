"""BACKEND_BUGS B-152: a routine row named its assignees only as display text.

`assigned_to` is `[str(user)]` = "samer (Client)" — a username and a translated user-type
label, not an id — so the app matched a client to a routine only through
`assigned_usernames`, after a second call to learn the username. `assigned_ids` is the
same list as ids, under the same privacy rule: the author or an admin sees every
assignee, another trainer only their own approved clients, a client only themselves.
"""
import pytest

pytestmark = pytest.mark.django_db

LIST = "/api/routine/routines/"


def _row(resp, routine):
    assert resp.status_code == 200, resp.content
    body = resp.json()
    rows = body["results"] if "results" in body else [body]
    return next(r for r in rows if r["id"] == routine.pk)


def test_routine_rows_carry_assignee_ids(make_user, api):
    from routine.models import Routine
    from users.models import TrainerClientRelation

    author = make_user("b152_author", user_type="trainer")
    other = make_user("b152_other", user_type="trainer")
    client = make_user("b152_client")
    theirs = make_user("b152_theirs")
    TrainerClientRelation.objects.create(trainer=author, client=client, status="approved",
                                         initiated_by="client")
    TrainerClientRelation.objects.create(trainer=other, client=theirs, status="approved",
                                         initiated_by="client")
    routine = Routine.objects.create(name="b152 program", created_by=author, days=2)
    routine.assigned_to.add(client, theirs)     # data from before one-client-per-routine

    mine = _row(api(author).get(LIST), routine)
    assert sorted(mine["assigned_ids"]) == sorted([client.pk, theirs.pk])
    assert _row(api(author).get(f"{LIST}{routine.pk}/"), routine)["assigned_ids"] == \
        mine["assigned_ids"]

    assert _row(api(client).get(LIST), routine)["assigned_ids"] == [client.pk]
    assert _row(api(client).get(f"{LIST}{routine.pk}/"), routine)["assigned_ids"] == \
        [client.pk]

    seen_by_other = _row(api(other).get(LIST), routine)
    assert seen_by_other["assigned_ids"] == [theirs.pk]
    assert seen_by_other["assigned_usernames"] == [theirs.username]
