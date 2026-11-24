"""The fix run's RTN-1, S-248 (2026-11-24; performance only): two reads joined a to-many
relation and undid the repeats with a DISTINCT over every selected column.

  GET routine-exercises/  `routine__assigned_to` OR-ed with authorship: a LEFT JOIN that
                          repeated each row once per assignee, then DISTINCT over rows
                          that select two whole user rows
  GET exercises/ (client) `routine_exercises__routine__assigned_to`: each exercise once
                          per row that used it, then DISTINCT

Both are now a subquery of ids: the same rows, each once, and no DISTINCT. The SQL tests
fail on the code before; the row tests hold the answer.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

pytestmark = pytest.mark.django_db


def _list(resp):
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body["results"] if isinstance(body, dict) else body


def _world(make_user):
    """A trainer's routine of one exercise assigned to three clients, and a second
    routine of one client's using the same exercise."""
    from routine.models import Exercise, Routine, RoutineExercise
    from users.models import TrainerClientRelation

    trainer = make_user("s248_t", user_type="trainer")
    clients = [make_user(f"s248_c{index}") for index in range(3)]
    move = Exercise.objects.create(name="s248 move", created_by=trainer)
    routine = Routine.objects.create(name="s248 a", created_by=trainer, days=1)
    row = RoutineExercise.objects.create(routine=routine, exercise=move, day=1, sets=3,
                                         reps=10)
    second = Routine.objects.create(name="s248 b", created_by=trainer, days=1)
    RoutineExercise.objects.create(routine=second, exercise=move, day=1, sets=3, reps=10)
    for client in clients:
        TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                             status="approved")
        routine.assigned_to.add(client)
    second.assigned_to.add(clients[0])
    return trainer, clients, move, routine, row


def _selects(sql, table):
    return [q for q in sql if q.lstrip().upper().startswith("SELECT") and table in q]


def test_the_trainers_routine_rows_come_once_without_distinct(make_user, api):
    trainer, _clients, _move, routine, row = _world(make_user)
    with CaptureQueriesContext(connection) as sql:
        rows = _list(api(trainer).get(f"/api/routine/routine-exercises/?routine={routine.pk}"))
    assert [r["id"] for r in rows] == [row.pk]
    listed = _selects([q["sql"] for q in sql.captured_queries], '"routine_routineexercise"')
    assert listed and not any("DISTINCT" in q.upper() for q in listed), listed


def test_a_clients_exercises_come_once_without_distinct(make_user, api):
    _trainer, clients, move, _routine, _row = _world(make_user)
    with CaptureQueriesContext(connection) as sql:
        rows = _list(api(clients[0]).get("/api/routine/exercises/?page_size=100"))
    assert [r["id"] for r in rows].count(move.pk) == 1
    listed = _selects([q["sql"] for q in sql.captured_queries], '"routine_exercise"')
    assert listed and not any("SELECT DISTINCT" in q.upper() for q in listed), listed


def test_the_rule_reads_the_same(make_user):
    from routine.models import Exercise
    from routine.permissions import visible_exercises_for

    _trainer, clients, move, _routine, _row = _world(make_user)
    stranger = make_user("s248_stranger")
    other = make_user("s248_t2", user_type="trainer")
    private = Exercise.objects.create(name="s248 private", created_by=other)
    seen = set(visible_exercises_for(clients[0]).values_list("pk", flat=True))
    assert move.pk in seen and private.pk not in seen
    assert move.pk not in set(visible_exercises_for(stranger).values_list("pk", flat=True))
