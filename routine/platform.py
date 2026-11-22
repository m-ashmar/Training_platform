"""Writing the platform's own templates onto a database (`routine/data/platform_templates.py`).

`manage.py bootstrap_platform` calls `ensure_platform_templates()` after the starter
exercises, so a new server has the free first workouts (Road to 10 S2.4) and the starter
templates for trainers (S2.10) from its first deploy. Idempotent: a template whose key
exists is left exactly as the operator left it.
"""
from __future__ import annotations

from django.db import transaction


def ensure_platform_templates(templates=None) -> dict:
    """Write each platform template that is missing. Never updates or deletes one.

    Answers `{"created": [keys], "found": [keys], "missing_exercises": {key: [catalogue
    keys]}}`; a template whose exercises are not all in the database is not written.
    """
    from django.conf import settings

    from .data.platform_templates import PLATFORM_TEMPLATES
    from .models import Exercise, RoutineTemplate, RoutineTemplateExercise

    templates = PLATFORM_TEMPLATES if templates is None else templates
    report = {"created": [], "found": [], "missing_exercises": {}}
    present = set(RoutineTemplate.objects.filter(
        platform_key__in=[t.key for t in templates]).values_list("platform_key", flat=True))
    wanted = {row.exercise for t in templates for row in t.rows}
    exercises = {e.catalogue_key: e for e in Exercise.objects.filter(
        catalogue_key__in=wanted, is_active=True)}
    languages = [code for code, _label in settings.LANGUAGES]
    for entry in templates:
        if entry.key in present:
            report["found"].append(entry.key)
            continue
        absent = [row.exercise for row in entry.rows if row.exercise not in exercises]
        if absent:
            report["missing_exercises"][entry.key] = absent
            continue
        texts = {}
        for language in languages:
            texts[f"name_{language}"] = getattr(entry, f"name_{language}", "") or entry.name_en
            texts[f"description_{language}"] = (getattr(entry, f"description_{language}", "")
                                                 or entry.description_en)
        # Each day's rows in the order written, numbered from 1 within the day.
        order_in_day: dict = {}
        lines = []
        for row in entry.rows:
            order_in_day[row.day] = order_in_day.get(row.day, 0) + 1
            lines.append((row, order_in_day[row.day]))
        with transaction.atomic():
            template = RoutineTemplate.objects.create(
                **texts, goal=entry.goal, days=entry.days, weeks=entry.weeks,
                place=entry.place, is_public=entry.is_public, created_by=None,
                platform_key=entry.key)
            RoutineTemplateExercise.objects.bulk_create([
                RoutineTemplateExercise(template=template, exercise=exercises[row.exercise],
                                        sets=row.sets, reps=row.reps,
                                        rest_time=row.rest_time, day=row.day, order=order)
                for row, order in lines])
        report["created"].append(entry.key)
    return report
