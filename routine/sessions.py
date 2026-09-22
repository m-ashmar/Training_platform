"""What counts as a workout that is still running — and what becomes of one that is not.

`WorkoutSession.status` has always carried an `abandoned` value that nothing ever wrote
(SIDE_BUGS S-022). A client who taps "start", locks the phone and never comes back left
a row reading `active` for ever, so every screen asking "is a workout running" answered
yes until somebody edited the database by hand. The home screen worked around it by
bounding a session at twelve hours on its own; that bound is the rule, and it lives here
so that the home screen, the session endpoints and the nightly sweep cannot disagree
about it.

Two halves, and both are needed:

* the READ rule — `running()` / `forgotten()` below. It is true the instant the twelfth
  hour passes, with no task having to have run, which is what makes the sweep's timing
  a matter of housekeeping rather than of correctness.
* the WRITE — `routine.tasks.abandon_forgotten_sessions`, nightly, which makes the
  column say what the read rule already knows.

Nothing here reads or writes a set log. See the task's docstring for what happens to the
work logged inside a session that is abandoned: the short answer is nothing at all.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

#: The longest a single workout may be counted as running, in seconds. Generous — an
#: all-day event is twelve hours — but finite, which is the whole point: past it a
#: session was forgotten, not left running, and without a bound one forgotten session
#: holds a client's start button on "continue" for the rest of their life.
#:
#: The same number bounds a session's recorded length on a finish
#: (`routine/serializers.py` `WorkoutSessionSerializer.MAX_SESSION_SECONDS`, which is an
#: alias of this) and is what `home/sections.py LONGEST_SESSION` now points at.
SESSION_MAX_SECONDS = 12 * 3600


def cutoff(now=None):
    """The moment a session must have begun AFTER to still be called running."""
    return (now or timezone.now()) - timedelta(seconds=SESSION_MAX_SECONDS)


def running(queryset, *, now=None):
    """The sessions of `queryset` a client would call open: `active`, and begun inside
    `SESSION_MAX_SECONDS`.

    Callers pass their own already-scoped queryset — this adds no authorisation of its
    own and must never be handed `WorkoutSession.objects.all()` on a read path.
    """
    return queryset.filter(status='active', start_time__gte=cutoff(now))


def forgotten(queryset, *, now=None):
    """The mirror of `running()`: rows still marked `active` that nobody is training.

    What the sweep closes.
    """
    return queryset.filter(status='active', start_time__lt=cutoff(now))
