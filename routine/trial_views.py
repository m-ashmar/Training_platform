"""`/api/routine/trial-workout/` — the free first workout (Road to 10 S2.4, the owner's D10).

The rules are `routine/trial.py`'s; this is the door to them.
"""
from __future__ import annotations

from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import trial


def _refusal(reason):
    if reason == 'not_a_client':
        return Response({'error': _('The free first workout is for clients.'),
                         'code': 'not_a_client'}, status=status.HTTP_403_FORBIDDEN)
    return Response({'error': _('The free first workout is not offered to you now.'),
                     'code': 'trial_not_offered', 'reason': reason},
                    status=status.HTTP_404_NOT_FOUND)


class TrialWorkoutView(APIView):
    """The free first workout: a platform workout of about 20 minutes that needs no
    equipment, for a client with no coach and no programme (Road to 10 S2.4, the
    owner's D10). The home section `trial_workout` carries the same object.

    Any signed-in client; 401 without a token. No body, no headers needed. **Free on
    every build and sells nothing**: no price, no purchase, the same answer on `apk`,
    `play`, `ios` and `web`.

    ``GET`` — what is offered. 200::

        {"template_id": 31,              the platform's workout
         "routine_id": 88 | null,        the person's OWN copy, once started (null before)
         "day": 1,                       the day to open (the copy has one day)
         "place": "home" | "gym",        which one: the place given at onboarding
                                         (`training_place`), home when unanswered; once
                                         started, the place the copy was written for
         "name": "Full body in 20 minutes", "description": "…",   reader's language
         "exercises": 5, "sets": 11, "estimated_minutes": 20,
                                         the routine's own formula (`day_minutes`), the
                                         number Home and Train show for any day
         "equipment": ["bodyweight"],    `EQUIPMENT_CHOICES` codes its exercises name
         "no_equipment": true,           every exercise says `bodyweight`
         "muscles": ["Front Quads", …],  stored keys (`Exercise.target_muscle`), in order
         "last_done_on": "2026-11-21" | null,   the newest day it was closed (any way but
                                         skipped)
         "times_done": 1,
         "session_id": 91}               ONLY while a session on it is running

    ``POST`` — start it: writes the person's own copy of the platform's workout (a
    private routine of one day, theirs, never assigned) and answers the object above with
    `routine_id` set: 201 the first time, 200 on every later call (the same copy; one per
    person). Then train it through the ORDINARY routes: `POST
    /api/routine/workout-sessions/ {routine: routine_id, day: 1}`, the sets, and
    finishing the session — it is kept in history like any other workout. It has no
    schedule (`routines/{id}/schedule/` answers 404: it is a workout, not a programme),
    it is not listed by `GET routines/` (read it by id), and its days do not count
    towards the streak.

    Refusals, on both methods::

        403 not_a_client         a trainer, an agent or an operator
        404 trial_not_offered    with `reason`:
                                   has_coach    an approved coaching, frozen or unpaid
                                                included (the freeze rule holds)
                                   has_program  a routine is assigned to them
                                   unavailable  this server has no trial workout
                                                (`manage.py bootstrap_platform` writes it)

    Once started, the copy is the person's for good: after a coach or a programme
    arrives it stays readable by id and trainable (a running session can be finished
    and a new one opened on it — it is free and sells nothing); only the OFFER goes
    (this route and the home section answer `trial_not_offered`). Its days stay out of
    every number, theirs and their coach's (`routine/outcomes._counted`).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            return Response(trial.offer(request.user))
        except trial.NotOffered as refused:
            return _refusal(refused.reason)

    def post(self, request):
        try:
            answer, created = trial.start(request.user)
        except trial.NotOffered as refused:
            return _refusal(refused.reason)
        return Response(answer, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
