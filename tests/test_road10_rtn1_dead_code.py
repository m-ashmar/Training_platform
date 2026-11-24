"""The fix run's RTN-1 dead-code rows (2026-11-24). Each name read like a live path and was
not one; the next reader would have wired it in.

S-169  `UserRoutineSerializer` and `ExerciseMediaSerializer` were imported by
       `routine/views.py` and used nowhere there; `UserRoutineSerializer` was used
       nowhere at all and `WorkoutSessionDetailSerializer` was read by nothing. The two
       unused classes are deleted; `ExerciseMediaSerializer` stays, the nested `media` of
       the exercise serializers.
S-227  `Routine.can_be_accessed_by` had no caller and disagreed with
       `routine.permissions.readable_routines` (it refused an inactive routine and
       ignored an approved trainer). Deleted.
"""


def test_s169_the_views_import_only_what_they_use():
    from routine import views

    for name in ("UserRoutineSerializer", "ExerciseMediaSerializer"):
        assert not hasattr(views, name), name


def test_s169_the_unused_serializers_are_gone():
    from routine import serializers

    for name in ("UserRoutineSerializer", "WorkoutSessionDetailSerializer"):
        assert not hasattr(serializers, name), name
    assert hasattr(serializers, "ExerciseMediaSerializer")       # still nested in use


def test_s227_routine_has_one_access_rule():
    from routine.models import Routine

    assert not hasattr(Routine, "can_be_accessed_by")
