"""How a trainer's program becomes one client's private routine.

The owner's model (2026-09-19): a trainer saves a TEMPLATE (exercises, sets, reps) and
reuses it for as many clients as they like, personalising it per client before it is
assigned. Once assigned it is that client's personal routine. Nobody shares a routine:
two clients on the same template get two separate `Routine` rows, and editing the
template or one client's copy never changes another's.

Two ways in, one rule:
  * `routine_from_template` writes the private copy (the routine and its rows; no
    assignee, no notification, no money). `templates/{id}/generate/` calls it once per
    client, and so will routine products (plan step 10).
  * `assign_routine` puts ONE client on a routine and settles the coaching's first
    month, for `routines/{id}/assign_to_client/` and for `generate` alike. Before,
    only assign_to_client settled, so a first program delivered from a template left
    the coaching "awaiting_first_program" for ever and the trainer unpaid.

Lock order: the routine row is locked and released before settlement, which takes the
relation, user and wallet rows in the order `users/trainer_relations.py` documents. A
settlement failure never undoes the assignment; the hold stays on the relation and is
settled by the next assignment.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import override

logger = logging.getLogger(__name__)

#: Most clients one generate call may serve. Each gets its own routine and rows.
MAX_CLIENTS_PER_GENERATE = 50
#: Most routine-days (clients x days) one generate call may write. Assigning a routine
#: builds each day's progress row through the m2m receiver, about five queries a day:
#: 50 clients x 365 days was ~95k queries and minutes in one request.
MAX_ROUTINE_DAYS_PER_GENERATE = 1000

#: Bounds of a per-client customisation; the weight is `prescribed_weight`'s 0..1000.
CUSTOM_BOUNDS = {"sets": (1, 100), "reps": (1, 1000), "rest_time": (0, 3600)}


class RoutineTaken(Exception):
    """The routine is, or was, another client's: copy it instead (409
    `routine_belongs_to_another_client`)."""


class BadCustomization(ValueError):
    """A customisation value the columns cannot hold (400 `bad_customization`)."""

    def __init__(self, key, message):
        super().__init__(message)
        self.key = key
        self.message = message


def trained_by_someone_else(routine, client, *, check_assignees=True) -> bool:
    """True when another client is on the routine or has trained on it.

    Unassigning keeps the old client's history on the routine, so a routine handed on
    after an unassign carried that history into the next client's "private" program and
    into the trainer's completion figures for them. The routine's author is not "someone
    else": a trainer who tries out their own draft can still assign it.
    `check_assignees=False` asks about history only (the console, where the assignees
    being saved replace the current ones).
    """
    from .models import RoutineProgress, WorkoutSession

    others = {'user_id__in': [client.pk, routine.created_by_id]}
    return ((check_assignees and routine.assigned_to.exclude(pk=client.pk).exists())
            or WorkoutSession.objects.filter(routine=routine).exclude(**others).exists()
            or RoutineProgress.objects.filter(routine=routine).exclude(**others)
                                      .exclude(status='not_started').exists())


def assign_routine(routine, client, actor):
    """Put `client` on `routine`, then settle the coaching's first month.

    Raises `RoutineTaken` if another client is on it or has trained on it. The check
    and the add run under a lock on the routine row, so two assignments racing for
    different clients cannot both pass. The rows must already exist: the m2m receiver
    counts them into each day's `RoutineProgress.total_exercises`.
    """
    from .models import Routine

    with transaction.atomic():
        Routine.objects.select_for_update(of=('self',)).only('pk').get(pk=routine.pk)
        if trained_by_someone_else(routine, client):
            raise RoutineTaken(routine.pk)
        routine.assigned_to.add(client)
    settle_first_month(actor, client)


def settle_first_month(actor, client):
    """Pay out the month held for this pair, once. Nothing to do for an admin, or for a
    coaching whose first month is already settled (`settle_hold` is idempotent)."""
    from users.trainer_relations import HoldFailed, active_relation, settle_hold

    if not getattr(actor, 'is_trainer', False):
        return
    relation = active_relation(actor, client)
    if relation is None:
        return
    try:
        settle_hold(relation, actor)
    except HoldFailed as exc:
        # The routine is assigned either way; the hold is still on the relation, so the
        # next assignment retries the payment.
        logger.error("Settlement deferred for relation %s: %s", relation.id, exc)


def prescribed_weight(value):
    """A working weight in kg, or None if it is not a number from 0 to 1000."""
    if isinstance(value, bool):
        return None
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return None
    return weight if 0 <= weight <= 1000 else None


def clean_customizations(raw) -> dict:
    """`{exercise_id: {sets, reps, rest_time, weight}}` checked value by value.

    Unchecked, `{"12": {"sets": "abc"}}`, a negative count or a non-object answered 500
    from inside `bulk_create`. Keys are exercise ids as strings; unknown ids are ignored,
    as before.
    """
    if raw in (None, ''):
        return {}
    if not isinstance(raw, dict):
        raise BadCustomization('customizations', _("customizations must be an object."))
    cleaned = {}
    for key, values in raw.items():
        if not isinstance(values, dict):
            raise BadCustomization(str(key), _("Each customisation must be an object."))
        row = {}
        for field, (low, high) in CUSTOM_BOUNDS.items():
            if field not in values or values[field] is None:
                continue
            value = values[field]
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                value = None
            else:
                try:
                    value = int(str(value).strip())
                except ValueError:
                    value = None
            if value is None or not low <= value <= high:
                raise BadCustomization(
                    str(key), _("%(field)s must be a whole number from %(low)s to %(high)s.")
                    % {"field": field, "low": low, "high": high})
            row[field] = value
        if values.get('weight') not in (None, ''):
            weight = prescribed_weight(values['weight'])
            if weight is None:
                raise BadCustomization(str(key), _("Weight must be between 0 and 1000 kg."))
            row['weight'] = weight
        cleaned[str(key)] = row
    return cleaned


def _template_text(template, field, language):
    with override(language):
        return getattr(template, field) or ''


def routine_from_template(template, *, created_by, days, customizations=None,
                          name=None, start_date=None, end_date=None, source=True):
    """Write a private copy of `template` as a new routine with no assignee.

    `days` may be longer than the template: client day N uses template day
    ((N-1) % template.days) + 1. `customizations` must come from `clean_customizations`.
    Both languages of the template's name and description go into
    `Routine.translations`; `name` overrides the name in both.
    """
    from django.conf import settings

    from .models import Routine, RoutineExercise

    customizations = customizations or {}
    default = settings.LANGUAGE_CODE
    texts = {language: {'name': name or _template_text(template, 'name', language),
                        'description': _template_text(template, 'description', language)}
             for language, _label in settings.LANGUAGES}
    # The default language lives in the columns only; the others in `translations`,
    # which every edit of the name or description clears (`Routine.forget_translations`)
    # so an edit is never hidden behind a stale copy.
    translations = {language: text for language, text in texts.items() if language != default}

    rows = list(template.routinetemplateexercise_set.select_related('exercise').all())
    cycle = template.days or 1
    fresh, seen = [], set()
    for client_day in range(1, days + 1):
        template_day = ((client_day - 1) % cycle) + 1
        for row in rows:
            # A template written before rows were validated may hold the same
            # (exercise, day, order) twice; the routine's unique key refused the second
            # and the whole generate answered 500.
            key = (row.exercise_id, client_day, row.order)
            if row.day != template_day or key in seen:
                continue
            seen.add(key)
            custom = customizations.get(str(row.exercise_id), {})
            fresh.append(RoutineExercise(
                exercise=row.exercise,
                sets=custom.get('sets', row.sets),
                reps=custom.get('reps', row.reps),
                rest_time=custom.get('rest_time', row.rest_time),
                weight=custom.get('weight'),
                day=client_day,
                order=row.order,
            ))

    # The columns hold the default language; `TranslatedJSONFieldMixin` answers any
    # other language from `translations`. The request's language used to go into the
    # columns, so an Arabic trainer's copy read Arabic to an English client and nothing
    # at all was stored for the other language.
    values = {
        'name': texts[default]['name'] or name or template.name,
        'description': texts[default]['description'],
        'translations': translations,
        'days': days,
        'created_by': created_by,
        'source_template': template if source else None,
    }
    if start_date:
        values['start_date'] = start_date
    if end_date:
        values['end_date'] = end_date
    with transaction.atomic():
        routine = Routine.objects.create(**values)
        for row in fresh:
            row.routine = routine
        if fresh:
            RoutineExercise.objects.bulk_create(fresh)
    return routine


def copy_routine(original, **overrides):
    """A fresh, unassigned copy of `original`: same days, rows and translations, with
    the day names, supersets and load modes (B-104) — «ابدأ من برنامجه الحالي» must not
    flatten the program it starts from.

    For `routines/{id}/copy/` and the console's "duplicate" action, which copied only
    the vestigial `exercises` list and so produced a routine with no days at all.
    """
    from .models import Routine, RoutineExercise

    values = dict(name=original.name, description=original.description,
                  translations=original.translations, created_by=original.created_by,
                  days=original.days, difficulty_level=original.difficulty_level,
                  estimated_duration=original.estimated_duration, is_active=True,
                  source_template=original.source_template,
                  day_names=dict(original.day_names or {}))
    values.update(overrides)
    values['translations'] = Routine.forget_translations(
        values.get('translations'), *[f for f in ('name', 'description') if f in overrides])
    with transaction.atomic():
        copy = Routine.objects.create(**values)
        RoutineExercise.objects.bulk_create([
            RoutineExercise(routine=copy, exercise_id=rex.exercise_id, sets=rex.sets,
                            reps=rex.reps, rest_time=rex.rest_time, day=rex.day,
                            order=rex.order, weight=rex.weight, duration=rex.duration,
                            distance=rex.distance, notes=rex.notes,
                            superset_group=rex.superset_group, load_mode=rex.load_mode,
                            load_value=rex.load_value)
            for rex in original.routine_exercises.all()])
        copy.exercises.set(original.exercises.all())
    return copy

