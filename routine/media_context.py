"""Resolving a page's exercise media once, instead of once per exercise.

`ExerciseSerializer` is nested three deep in places (routine -> routine_exercise ->
exercise) and a nested serializer cannot see its siblings, so left alone it would ask
"whose demonstration?" one row at a time. That is the N+1 this module exists to
prevent: a view names every exercise on the page BEFORE anything is serialised, and
the serializer then answers from memory.

Two entries go into the serializer context:

  `exercise_media`          a `mediakit.resolution.ExerciseMediaCache`
  `exercise_media_authors`  {routine id: created_by id}, so a row can name the coach
                            who prescribed it without a query of its own

and a third, `exercise_media_author`, is set and cleared around each routine row by
`RoutineExerciseSerializer.to_representation` — line (a) of the rule is per routine,
and the exercise object itself does not know which routine it was read out of.

A view that serialises exercises and does NOT use `ExerciseMediaMixin` still answers
correctly; it just pays a query per exercise. The query-count test in
`tests/test_trainer_exercise_media.py` is what holds the wired ones honest.
"""
from __future__ import annotations

CACHE_KEY = "exercise_media"
AUTHORS_KEY = "exercise_media_authors"
#: Set per row while a routine's exercises are being rendered. Absent everywhere else.
ROW_AUTHOR_KEY = "exercise_media_author"


def _walk(instances, exercises_by_routine, routine_ids):
    """Collect (routine id or None) -> exercise ids from whatever the view serialises.

    Five shapes reach a serializer in this app. Handled by `isinstance` rather than
    by duck-typing because `hasattr(obj, 'exercise')` is true of three of them and
    means something different in each.
    """
    from .models import (Exercise, RoutineExercise, Routine, RoutineTemplate,
                         RoutineTemplateExercise, UserExerciseProgress)

    for obj in instances:
        if isinstance(obj, Exercise):
            exercises_by_routine.setdefault(None, set()).add(obj.pk)
        elif isinstance(obj, RoutineExercise):
            if obj.exercise_id:
                exercises_by_routine.setdefault(obj.routine_id, set()).add(obj.exercise_id)
                routine_ids.add(obj.routine_id)
        elif isinstance(obj, Routine):
            ids = {r.exercise_id for r in obj.routine_exercises.all() if r.exercise_id}
            if ids:
                exercises_by_routine.setdefault(obj.pk, set()).update(ids)
                routine_ids.add(obj.pk)
        elif isinstance(obj, RoutineTemplate):
            # A template is NOT a routine: nobody prescribed it to the person reading
            # it, and a public template's author is a stranger. Line (a) does not
            # apply, so these go in unattributed and the reader sees their own coach's
            # media or the catalogue's — never a stranger trainer's footage.
            ids = {r.exercise_id for r in obj.routinetemplateexercise_set.all()
                   if r.exercise_id}
            if ids:
                exercises_by_routine.setdefault(None, set()).update(ids)
        elif isinstance(obj, (RoutineTemplateExercise, UserExerciseProgress)):
            if obj.exercise_id:
                exercises_by_routine.setdefault(None, set()).add(obj.exercise_id)


def prepare(request, instances) -> dict:
    """The two context entries for this page. One query for the authors, then the rule."""
    from django.db.models import Model

    from mediakit.resolution import ExerciseMediaCache

    from .models import Routine

    if instances is None:
        instances = []
    elif isinstance(instances, Model):
        instances = [instances]
    else:
        # A page is a list, a detail is one row, and a viewset can hand over a
        # queryset. `list()` of a prefetched queryset reuses the prefetch.
        instances = list(instances)

    exercises_by_routine: dict = {}
    routine_ids: set = set()
    _walk(instances, exercises_by_routine, routine_ids)

    authors = dict(Routine.objects.filter(pk__in=routine_ids)
                   .values_list("pk", "created_by_id")) if routine_ids else {}

    cache = ExerciseMediaCache(getattr(request, "user", None), request)
    for routine_id, ids in exercises_by_routine.items():
        cache.want(ids, authors.get(routine_id) if routine_id else None)
    return {CACHE_KEY: cache, AUTHORS_KEY: authors}


class ExerciseMediaMixin:
    """Mix into any viewset whose serializer nests `ExerciseSerializer`.

    Hooks `get_serializer` rather than `get_serializer_context`, because the context
    is built before DRF hands over the page and the page is what has to be named.
    """

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        instances = args[0] if args else None
        if instances is not None and not isinstance(instances, dict):
            try:
                serializer.context.update(prepare(self.request, instances))
            except (TypeError, AttributeError):
                # A serializer built around raw data rather than model instances (a
                # write path). Nothing to name; the per-exercise fallback still
                # answers correctly, and a failure here must never fail the write.
                pass
        return serializer
