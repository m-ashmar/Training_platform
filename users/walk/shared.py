"""What several walk areas use. Every step here is a real route or service."""
from __future__ import annotations


def coach_client(walk, client, coach):
    """`client` asks `coach` and `coach` accepts, through the two routes the app uses
    (`client/request-trainer/`, `trainer/respond-to-request/`). With a coach who charges
    nothing no money moves and nothing is held; a state that needs a paid month gives
    the coach a fee and the client the money first (the money worker's states).
    Answers the approved relation."""
    from users.models import TrainerClientRelation

    walk.api(client, "post", "/api/auth/client/request-trainer/", {"trainer_id": coach.pk})
    relation = TrainerClientRelation.objects.get(trainer=coach, client=client)
    walk.api(coach, "post", "/api/auth/trainer/respond-to-request/",
             {"request_id": relation.pk, "action": "approve"})
    relation.refresh_from_db()
    client.refresh_from_db()
    return relation


def starter_exercise(walk, offset: int = 0):
    """A global catalogue exercise (`bootstrap_platform`), the `offset`-th by id."""
    from routine.models import Exercise

    walk.require_bootstrap()
    return (Exercise.objects.filter(is_global=True, is_active=True)
            .exclude(catalogue_key__isnull=True).exclude(catalogue_key="")
            .order_by("id")[offset])


def sync_sets(walk, user, exercise, day, count: int, *, reps=10, weight=40,
              session=None):
    """`count` sets of `exercise` on `day`, through `POST set-logs/sync/` exactly as the
    phone sends them (its own `client_uuid` per set, the day it was trained)."""
    import uuid

    rows = []
    for number in range(1, count + 1):
        row = {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
               "set_number": number, "reps": reps, "weight": weight,
               "date": day.isoformat()}
        if session is not None:
            row["workout_session_id"] = session.pk
        rows.append(row)
    _status, body = walk.api(user, "post", "/api/routine/set-logs/sync/", {"sets": rows},
                             expect=(200, 201, 207))
    refused = [r for r in (body or {}).get("results", []) if r.get("status") == "error"]
    if refused:
        from .framework import WalkApiRefused

        raise WalkApiRefused(f"set-logs/sync/ refused {refused}")
    return body
