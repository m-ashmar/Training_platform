"""The free first workout (Road to 10 S2.4, the owner's D10, 2026-11-22).

A client with no coach and no programme is offered ONE platform workout of about twenty
minutes that needs no equipment, the one for the place they gave at onboarding
(`CustomUser.training_place`: home or gym; home when they have not said, because it
needs nothing at all). It is served by the server so it can change without a release,
and it is logged like any other workout.

THE RULES IT KEEPS

* **A routine is one person's private copy** (`routine/assignment.py`). The platform's
  workout is a TEMPLATE with no author (`routine/data/platform_templates.py`, keys
  `trial-home` and `trial-gym`); starting it writes the person's OWN `Routine` from it
  (`routine_from_template`, `trial=True`), once — a second start answers the same copy.
  Nobody ever trains on the template, and nobody shares a copy.
* **Logged through the ordinary routes, kept in history.** The copy is `created_by` the
  person, so `workout-sessions/`, `set-logs/`, `user-exercise-progress/` and the day's
  outcome take it exactly as they take any routine of theirs, and the session stays in
  their history. `training_refusal` answers None for it: no coaching owns it, so no
  freeze can stop it.
* **Without a programme the person later loses.** The copy is never ASSIGNED: the
  assignment is what makes a routine a programme (home's `workout_hero`, the schedule,
  the workout reminders, the nightly sweep, the coach's lists). So the first-day home
  stays the first-day home, and nothing is taken away later.
* **Without a streak.** Its days are not training days for the streak or the week tile
  (`routine/outcomes._counted`).
* **The freeze rule holds.** It is offered only with no approved coach (a frozen or
  unpaid coaching is still a coach) and no programme (a former client's frozen routine is
  still their programme): the trial is never a way round a frozen month.
* **Free on every build; sells nothing.** No price, no purchase, no `money_type`, and the
  answer is the same on `apk`, `play`, `ios` and `web`.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

#: Where the person trains -> the platform template written for it.
TRIAL_KEYS = {'home': 'trial-home', 'gym': 'trial-gym'}
#: The place assumed when onboarding has not answered: the home workout needs nothing.
DEFAULT_PLACE = 'home'


class NotOffered(Exception):
    """The trial is not for this person now. `reason` is the refusal's `reason`."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def place_of(person) -> str:
    place = getattr(person, 'training_place', '') or ''
    return place if place in TRIAL_KEYS else DEFAULT_PLACE


def template_for(person):
    """`(template, place)` — the platform's trial workout for this person's place, or
    the other one when the operator has removed theirs; `(None, None)` when this server
    has neither (`bootstrap_platform` was not run)."""
    from .models import RoutineTemplate

    found = {t.platform_key: t for t in RoutineTemplate.objects.filter(
        platform_key__in=TRIAL_KEYS.values(), created_by__isnull=True)}
    first = place_of(person)
    for place in (first, *[p for p in TRIAL_KEYS if p != first]):
        template = found.get(TRIAL_KEYS[place])
        if template is not None:
            return template, place
    return None, None


def copy_of(person):
    """The person's own copy of the trial workout, or None before they started it."""
    from .models import Routine

    return Routine.objects.filter(created_by=person, trial=True).first()


def not_offered_reason(person, *, coached=None, programme=None):
    """Why the trial is not offered to this person, or None when it is.

    `has_coach`: an approved coaching (frozen or unpaid included); `has_program`: a
    routine is assigned to them (their programme, a bought one, a former coach's frozen
    one). Callers that already know either pass it (`home/views.py`).
    """
    if coached is None:
        from users.models import TrainerClientRelation

        coached = TrainerClientRelation.objects.filter(client=person,
                                                       status='approved').exists()
    if coached:
        return 'has_coach'
    if programme is None:
        from .models import Routine

        programme = Routine.objects.filter(assigned_to=person, is_active=True).exists()
    if programme:
        return 'has_program'
    return None


def describe(person, *, copy=None, template=None, place=None) -> dict:
    """The trial workout as Home and Train draw it. From the person's COPY once they
    have one (it is what they train; the operator may edit the template since), else
    from the template."""
    from django.db.models import Count, Max

    from routine.models import EQUIPMENT_CHOICES, DayOutcome, WorkoutSession
    from routine.schedule import day_minutes

    from .sessions import running

    if copy is not None:
        rows = list(copy.routine_exercises.filter(day=1).select_related('exercise')
                    .order_by('order', 'id'))
        # The reader's language: `translations` first, then the column — the rule every
        # routine read applies (`TranslatedJSONFieldMixin`).
        name = _localised(copy, 'name') or ''
        description = _localised(copy, 'description') or ''
        template_id = copy.source_template_id
    else:
        rows = list(template.routinetemplateexercise_set.filter(day=1)
                    .select_related('exercise').order_by('order', 'id'))
        name, description = template.name, template.description or ''
        template_id = template.pk

    codes = [code for code, _label in EQUIPMENT_CHOICES]
    said = {row.exercise.equipment for row in rows if row.exercise.equipment}
    muscles = []
    for row in rows:
        muscle = row.exercise.target_muscle
        if muscle and muscle not in muscles:
            muscles.append(muscle)
    answer = {
        'template_id': template_id,
        'routine_id': copy.pk if copy is not None else None,
        'day': 1,
        'place': place,
        'name': name,
        'description': description,
        'exercises': len(rows),
        'sets': sum(max(row.sets or 1, 1) for row in rows),
        'estimated_minutes': day_minutes(rows),
        'equipment': [code for code in codes if code in said],
        'no_equipment': bool(rows) and all(row.exercise.equipment == 'bodyweight'
                                           for row in rows),
        'muscles': muscles,
        'last_done_on': None,
        'times_done': 0,
    }
    if copy is not None:
        done = (DayOutcome.objects.filter(user=person, routine=copy).exclude(via='skipped')
                .aggregate(last=Max('date'), n=Count('id')))
        answer['last_done_on'] = done['last']
        answer['times_done'] = done['n'] or 0
        session = (running(WorkoutSession.objects.filter(user=person, routine=copy))
                   .order_by('-start_time', '-id').values_list('pk', flat=True).first())
        if session is not None:
            answer['session_id'] = session
    return answer


def _localised(routine, field):
    from django.utils.translation import get_language

    translations = routine.translations if isinstance(routine.translations, dict) else {}
    text = (translations.get(get_language() or 'en') or {}).get(field)
    return text or getattr(routine, field, None)


def offer(person, *, coached=None, programme=None) -> dict:
    """The trial workout for this person. Raises `NotOffered` when it is not for them."""
    if not getattr(person, 'is_client', False):
        raise NotOffered('not_a_client')
    reason = not_offered_reason(person, coached=coached, programme=programme)
    if reason:
        raise NotOffered(reason)
    copy = copy_of(person)
    template, place = template_for(person)
    if copy is None and template is None:
        raise NotOffered('unavailable')
    if copy is not None:
        # The place the copy was written for, which a later onboarding edit does not move.
        source = copy.source_template
        key = getattr(source, 'platform_key', None)
        place = next((p for p, k in TRIAL_KEYS.items() if k == key), place or place_of(person))
    return describe(person, copy=copy, template=template, place=place)


def start(person):
    """`(answer, created)`: the person's own copy of the trial workout, written on the
    first call and answered again on every later one. Raises `NotOffered`."""
    from training_platform.signals import bust_home_on_commit

    from .assignment import routine_from_template

    offered = offer(person)
    if offered['routine_id'] is not None:
        return offered, False
    template, place = template_for(person)
    from .models import Routine

    try:
        with transaction.atomic():
            copy = routine_from_template(template, created_by=person,
                                         days=max(template.days or 1, 1), trial=True)
            Routine.objects.filter(pk=copy.pk).update(
                estimated_duration=max(offered['estimated_minutes'], 1))
    except IntegrityError:
        # Two starts at once: the other one wrote it (`routine_one_trial_per_person`).
        copy = copy_of(person)
        if copy is None:
            raise
        return offer(person), False
    bust_home_on_commit(person.pk)
    return describe(person, copy=copy_of(person), template=template, place=place), True
