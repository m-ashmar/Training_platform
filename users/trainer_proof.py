"""What the platform itself can vouch for about a trainer.

The trainer cards show three numbers the trainer cannot type in: how fast they usually
answer a request, how many clients finished a program they wrote, and whether they are
taking clients. Everything here is computed from platform records on read, identically
for every viewer and every build, and in one query per statistic for a whole page of
trainers.

    typical_response_minutes   median time from a client's request to the trainer's
                               answer, over the last `RESPONSE_WINDOW_DAYS`, from
                               `TrainerRequestOutcome`; null below `MIN_RESPONSE_SAMPLE`
                               answers, because two numbers are not a habit
    response_sample_size       how many answers that median is taken over
    clients_completed_program  clients who completed a workout session for EVERY day of
                               a routine this trainer wrote for them. A routine is the
                               program as the platform stores it (`Routine.days` days)
    accepting_clients          the trainer's own switch (`trainer_is_available`)
    verified                   set by an operator (`trainer_is_verified`); the
                               verification flow itself is plan step 6

`experience_years` and certifications are the trainer's own claims and are shown beside
this, never inside it.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median

from django.db.models import Count, F
from django.utils import timezone

RESPONSE_WINDOW_DAYS = 90
MIN_RESPONSE_SAMPLE = 3


def response_times(trainer_ids) -> dict[int, tuple[int | None, int]]:
    """{trainer_id: (typical_response_minutes or None, sample_size)}."""
    from .models import TrainerRequestOutcome

    ids = list(trainer_ids)
    seconds: dict[int, list[float]] = {i: [] for i in ids}
    since = timezone.now() - timedelta(days=RESPONSE_WINDOW_DAYS)
    for trainer_id, asked, answered in (
            TrainerRequestOutcome.objects
            .filter(trainer_id__in=ids, initiated_by='client', responded_at__gte=since)
            .values_list('trainer_id', 'requested_at', 'responded_at')):
        seconds[trainer_id].append((answered - asked).total_seconds())
    out = {}
    for trainer_id, samples in seconds.items():
        if len(samples) < MIN_RESPONSE_SAMPLE:
            out[trainer_id] = (None, len(samples))
        else:
            # Rounded up: "within 2 hours" must never be said of 2 hours 20.
            out[trainer_id] = (max(1, -(-int(median(samples)) // 60)), len(samples))
    return out


def completed_programs(trainer_ids) -> dict[int, int]:
    """{trainer_id: clients who completed every day of one of this trainer's routines}."""
    from routine.models import WorkoutSession

    ids = list(trainer_ids)
    clients: dict[int, set] = {i: set() for i in ids}
    rows = (WorkoutSession.objects
            .filter(routine__created_by_id__in=ids, status='completed',
                    user__user_type='client', day__isnull=False, day__gte=1,
                    day__lte=F('routine__days'))
            .values('routine__created_by_id', 'routine_id', 'user_id', 'routine__days')
            .annotate(days_done=Count('day', distinct=True))
            .filter(days_done__gte=F('routine__days')))
    for row in rows:
        clients[row['routine__created_by_id']].add(row['user_id'])
    return {trainer_id: len(people) for trainer_id, people in clients.items()}


def proof_for(trainers) -> dict[int, dict]:
    """{trainer_id: proof dict} for a page of trainer rows (CustomUser)."""
    trainers = list(trainers)
    ids = [t.pk for t in trainers]
    timings = response_times(ids)
    finished = completed_programs(ids)
    out = {}
    for t in trainers:
        minutes, sample = timings.get(t.pk, (None, 0))
        out[t.pk] = {
            'verified': bool(t.trainer_is_verified),
            'accepting_clients': bool(t.trainer_is_available),
            'typical_response_minutes': minutes,
            'response_sample_size': sample,
            'clients_completed_program': finished.get(t.pk, 0),
        }
    return out
