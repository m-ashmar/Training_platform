"""The fix run's ACC-2, S-253 (2026-11-24): a person's export of their routines carries
no other person's id. `routines_created` declared no fields, so the export attached every
many-to-many, `assigned_to` included, and a legacy routine shared by two clients put each
one's id in the other's export. Nothing makes a shared routine any more; old rows did.
The first test fails on the code before.

Corrected for landing 2a: S-253 first dropped `assigned_to` from the export, which
reversed S-214's pinned `assigned_to == [the client's id]`
(`tests/test_fx_w1_privacy.py`). The rule now holds both: `assigned_to` carries the
reader's OWN id only (`PersonalDataSource.links_to_self`) — a client reads `[their id]`,
the trainer who wrote it `[]`, and nobody reads another person's id. The assertions
below changed from "the key is absent" to that, deliberately.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db


def _name(tag):
    return f"s253_{tag}_{uuid.uuid4().hex[:6]}"


def _rows(person):
    from training_platform.privacy import export_user_data

    return export_user_data(person)["sections"]["routines_created"]


def test_a_shared_routine_does_not_name_the_other_client(make_user):
    from routine.models import Routine

    trainer = make_user(_name("t"), user_type="trainer")
    first, second = make_user(_name("a")), make_user(_name("b"))
    legacy = Routine.objects.create(name="Shared, from before copies", created_by=trainer)
    legacy.assigned_to.add(first, second)

    for person, other in ((first, second), (second, first)):
        (row,) = [r for r in _rows(person) if r.get("id") == legacy.pk]
        assert row["assigned_to"] == [person.pk]
        assert other.pk not in [v for value in row.values() if isinstance(value, list)
                                for v in value]
    (mine,) = [r for r in _rows(trainer) if r.get("id") == legacy.pk]
    assert mine["assigned_to"] == []


def test_the_routine_and_its_exercises_are_still_exported(make_user):
    from routine.models import Exercise, Routine

    trainer = make_user(_name("t2"), user_type="trainer")
    client = make_user(_name("c"))
    routine = Routine.objects.create(name="Mine", created_by=trainer, days=2)
    routine.assigned_to.add(client)
    # `Routine.exercises`, the routine's own many-to-many (the day-by-day rows are the
    # `routine_exercises` section): still attached, as a list of ids.
    move = Exercise.objects.create(name=f"Row {uuid.uuid4().hex[:4]}", created_by=trainer)
    routine.exercises.add(move)

    (row,) = [r for r in _rows(client) if r.get("id") == routine.pk]
    assert (row["name"], row["days"], row["created_by"]) == ("Mine", 2, trainer.pk)
    assert row["exercises"] == [move.pk]
