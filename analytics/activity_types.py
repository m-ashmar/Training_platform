"""The names a `UserActivity.activity_type` may hold.

One vocabulary, because there were two. `analytics/recorder.py` is the only server-side
writer of `UserActivity` and it writes `routine_completed`, `exercise_completed`,
`diet_plan_generated` and `meal_completed`. `social/services.py` reads
`workout_completed` and `meal_logged` — names nothing has ever written. Every workout,
streak, milestone, late-night and early-morning achievement in the social app was
therefore unearnable, and silently so: the queries return 0, not an error.

`achievements/engine.py` hit the same wall and was corrected in place, which fixed one
reader and left the other. A free-text column shared by a writer and three readers needs
a name they all import.
"""
from __future__ import annotations

ROUTINE_COMPLETED = "routine_completed"
EXERCISE_COMPLETED = "exercise_completed"
DIET_PLAN_GENERATED = "diet_plan_generated"
MEAL_COMPLETED = "meal_completed"

#: Everything the server writes. `record_activity` refuses anything else, loudly.
ALL = frozenset({ROUTINE_COMPLETED, EXERCISE_COMPLETED,
                 DIET_PLAN_GENERATED, MEAL_COMPLETED})

#: "The client trained." A finished routine and a finished individual exercise both count.
WORKOUT = (ROUTINE_COMPLETED, EXERCISE_COMPLETED)

#: "The client ate something they had planned." There is no separate manual food log;
#: completing a meal IS the log.
MEAL = (MEAL_COMPLETED,)
