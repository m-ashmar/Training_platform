"""BACKEND_BUGS B-136: `GET /api/routine/routines/` took 3-20 s for ONE routine.

The list joined two to-many relations (`assigned_to` and `progress`) under a GROUP BY for
three `COUNT(DISTINCT ...)` annotations, selected two whole user rows (the author, and a
bought copy's product trainer: password hashes included) and, for a trainer,
`readable_routines` added `.distinct()` over every one of those ~160 columns. The
planner's row estimate for that fan-out passed `jit_above_cost` and PostgreSQL spent
seconds compiling a query whose nodes finished in 1.4 ms.

What the list must be now: a plain query. No DISTINCT, no GROUP BY, no join to the
progress rows (they are counted in correlated subqueries), no user password column,
and the numbers the screens read unchanged.
"""
import re

import pytest
from cachalot.api import cachalot_disabled
from django.db import connection
from django.test.utils import CaptureQueriesContext

pytestmark = pytest.mark.django_db

LIST = "/api/routine/routines/"


def _page_query(queries):
    """The paged SELECT whose main table is `routine_routine` (not the through table)."""
    found = [q["sql"] for q in queries
             if re.match(r"\s*SELECT", q["sql"], re.I)
             and 'FROM "routine_routine" ' in q["sql"]
             and " LIMIT " in q["sql"]]
    assert len(found) == 1, found
    return found[0]


def _assert_plain(sql):
    assert not re.match(r"\s*SELECT\s+DISTINCT", sql, re.I), sql
    assert "GROUP BY" not in sql, sql
    assert 'JOIN "routine_routineprogress"' not in sql, sql
    assert '"users_customuser"."password"' not in sql, sql


def _program(make_user):
    from routine.models import Routine, RoutineProgress
    from users.models import TrainerClientRelation

    trainer = make_user("b136_trainer", user_type="trainer")
    client = make_user("b136_client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    routine = Routine.objects.create(name="b136 program", created_by=trainer, days=6)
    routine.assigned_to.add(client)          # scaffolds one progress row per day
    rows = list(RoutineProgress.objects.filter(routine=routine).order_by("day", "id"))
    assert len(rows) == 6
    RoutineProgress.objects.filter(pk__in=[r.pk for r in rows[:3]]).update(status="completed")
    return trainer, client, routine


def test_routine_list_is_a_plain_query_for_trainer_and_client(make_user, api):
    trainer, client, routine = _program(make_user)

    with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
        resp = api(trainer).get(LIST)
    assert resp.status_code == 200, resp.content
    _assert_plain(_page_query(queries.captured_queries))
    row = next(r for r in resp.json()["results"] if r["id"] == routine.pk)
    assert row["client_count"] == 1
    assert row["completion_rate"] == 50.0

    with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
        resp = api(client).get(LIST)
    assert resp.status_code == 200, resp.content
    _assert_plain(_page_query(queries.captured_queries))
    row = next(r for r in resp.json()["results"] if r["id"] == routine.pk)
    assert row["client_count"] == 1
    assert row["created_by"] == str(trainer)


def test_a_trainer_still_sees_each_routine_once_and_only_readable_ones(make_user, api):
    """The DISTINCT is gone, so the rows it used to fold must not come back twice: a
    routine the trainer wrote AND that their client is on, listed once; another
    trainer's routine for a client of theirs, listed; a stranger's, not."""
    from routine.models import Routine
    from users.models import TrainerClientRelation

    trainer, client, mine = _program(make_user)
    second = make_user("b136_second")
    TrainerClientRelation.objects.create(trainer=trainer, client=second, status="approved",
                                         initiated_by="client")
    mine.assigned_to.add(second)             # two assignees: the old fan-out
    other = make_user("b136_other", user_type="trainer")
    theirs = Routine.objects.create(name="b136 theirs", created_by=other, days=2)
    theirs.assigned_to.add(client)
    stranger = Routine.objects.create(name="b136 stranger", created_by=other, days=2)
    stranger.assigned_to.add(make_user("b136_nobody"))

    body = api(trainer).get(LIST).json()
    ids = [r["id"] for r in body["results"]]
    assert ids.count(mine.pk) == 1
    assert theirs.pk in ids
    assert stranger.pk not in ids
    assert body["count"] == len(ids)
