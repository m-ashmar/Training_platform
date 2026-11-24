"""A routine a trainer can deliver as a client's programme, for tests that need one.

Since the backlog's G5 (2026-11-24), `routine.assignment.assign_routine` refuses a routine
with no exercises (400 `routine_empty`) or a retired one (400 `routine_inactive`): an
empty or retired routine used to pay the trainer the coaching's held month 1. Many tests
delivered "the first programme" as a bare `Routine` with no rows only to trigger that
payout; they build it here instead, with one exercise on day 1, which is what a real
first programme has.
"""


def deliverable_routine(trainer, name="Month one", days=3):
    """A routine `trainer` wrote, `days` long, with one exercise on day 1 (3 x 10)."""
    from routine.models import Exercise, Routine, RoutineExercise

    routine = Routine.objects.create(name=name, created_by=trainer, days=days)
    exercise = Exercise.objects.create(name=f"{name} exercise {routine.pk}",
                                       created_by=trainer)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, sets=3,
                                   reps=10)
    return routine
